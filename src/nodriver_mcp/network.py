"""Connection-scoped CDP network diagnostics, including redirect extra-info queues."""

import asyncio
import base64
import json
from collections import OrderedDict, deque
from pathlib import Path

import nodriver as uc

from .protocol import json_value


def merge_headers(base, extra):
    merged = {name.lower(): (name, value) for name, value in base.items()}
    merged.update({name.lower(): (name, value) for name, value in extra.items()})
    return dict(merged.values())


class NetworkCollector:
    def __init__(self, tab, collect):
        self.tab, self.collect = tab, collect
        self.chains = OrderedDict()
        net = uc.cdp.network
        self.callbacks = [
            (net.RequestWillBeSent, self.request),
            (net.ResponseReceived, self.response),
            (net.LoadingFinished, self.finished),
            (net.LoadingFailed, self.failed),
            (net.RequestWillBeSentExtraInfo, self.request_extra),
            (net.ResponseReceivedExtraInfo, self.response_extra),
        ]

    def chain(self, request_id):
        key = str(request_id)
        if key not in self.chains:
            self.chains[key] = {"hops": [], "request": deque(), "response": deque()}
            # Retain late extra-info across preserved navigations without allowing
            # orphaned events or a long-running connection to grow without bound.
            while len(self.chains) > 4000:
                self.chains.popitem(last=False)
        self.chains.move_to_end(key)
        return self.chains[key]

    def current(self, request_id):
        hops = self.chain(request_id)["hops"]
        return hops[-1] if hops else None

    def reconcile(self, chain):
        for kind in ("request", "response"):
            for record in chain["hops"]:
                if record.get("_extra_expected") is None:
                    break
                if not record["_extra_expected"] or kind + "_extra_info" in record:
                    continue
                if not chain[kind]:
                    break
                event = chain[kind].popleft()
                info = json_value(event)
                info.pop("requestId", None)
                record[kind + "_extra_info"] = info
                name = "headers" if kind == "request" else "response_headers"
                record[name] = merge_headers(record.get(name, {}), info["headers"])
                if kind == "response":
                    record["wire_status"] = event.status_code

    def apply_response(self, record, response, expected):
        record.update(
            status=response.status,
            response_headers=json_value(response.headers),
            mime_type=response.mime_type,
            response_url=response.url,
            _extra_expected=expected,
        )

    def request(self, event):
        chain = self.chain(event.request_id)
        previous = chain["hops"][-1] if chain["hops"] else None
        redirect = previous is not None and event.redirect_response is not None
        if redirect:
            self.apply_response(
                previous, event.redirect_response, event.redirect_has_extra_info
            )
            previous.update(finished=True, redirected=True)
        record = {
            "id": str(event.request_id),
            "url": event.request.url,
            "method": event.request.method,
            "headers": json_value(event.request.headers),
            "post_data": event.request.post_data,
            "has_post_data": bool(
                event.request.has_post_data or event.request.post_data is not None
            ),
            "timestamp": float(event.timestamp),
            "type": event.type_.value if event.type_ else "unknown",
            "finished": False,
            "connection_active": True,
            "_extra_expected": None,
        }
        self.collect(record)
        record["redirect_chain"] = (
            previous["redirect_chain"]
            + [
                {
                    k: previous[k]
                    for k in ("reqid", "url", "method", "status")
                    if k in previous
                }
            ]
            if redirect
            else []
        )
        if redirect:
            previous["redirect_to"] = record["reqid"]
            record["redirect_from"] = previous["reqid"]
        chain["hops"].append(record)
        self.reconcile(chain)

    def response(self, event):
        record = self.current(event.request_id)
        if record is not None:
            self.apply_response(record, event.response, event.has_extra_info)
            self.reconcile(self.chain(event.request_id))

    def finished(self, event):
        record = self.current(event.request_id)
        if record is not None:
            record.update(finished=True, encoded_data_length=event.encoded_data_length)

    def failed(self, event):
        record = self.current(event.request_id)
        if record is not None:
            record.update(finished=True, error=event.error_text)
            # A failed request may have extra-info without responseReceived.
            if record["_extra_expected"] is None:
                record["_extra_expected"] = True
            self.reconcile(self.chain(event.request_id))

    def request_extra(self, event):
        chain = self.chain(event.request_id)
        chain["request"].append(event)
        self.reconcile(chain)

    def response_extra(self, event):
        chain = self.chain(event.request_id)
        chain["response"].append(event)
        self.reconcile(chain)

    async def enable(self):
        for event, callback in self.callbacks:
            self.tab.add_handler(event, callback)
        try:
            await self.tab.send(uc.cdp.network.enable())
        except BaseException:
            self.remove()
            raise

    def remove(self):
        for event, callback in self.callbacks:
            if callback in self.tab.handlers.get(event, []):
                self.tab.remove_handler(event, callback)
        for chain in self.chains.values():
            for record in chain["hops"]:
                record["connection_active"] = False
        self.chains.clear()


async def body_value(record, response):
    if not response and record.get("post_data") is not None:
        return "available", record["post_data"], False, ""
    if not response and not record.get("has_post_data", True):
        return "empty", "", False, ""
    if response and record.get("redirected"):
        return (
            "redirect",
            None,
            False,
            "Intermediate redirect bodies are not retained by Chrome.",
        )
    if response and record.get("error"):
        return "failed", None, False, record["error"]
    if response and not record.get("finished", False):
        return "pending", None, False, "The request has not finished."
    if not record.get("connection_active", True):
        return (
            "connection_retired",
            None,
            False,
            "The originating connection was retired; no lookup was sent.",
        )
    net = uc.cdp.network
    try:
        if response:
            text, encoded = await record["connection"].send(
                net.get_response_body(net.RequestId(record["id"]))
            )
        else:
            text = await record["connection"].send(
                net.get_request_post_data(net.RequestId(record["id"]))
            )
            encoded = False
        return "available" if text else "empty", text, encoded, ""
    except uc.ProtocolException as exc:
        reason = str(exc)
        lower = reason.lower()
        state = (
            "evicted"
            if "evict" in lower or "no resource with given identifier" in lower
            else "unavailable"
            if "no data found" in lower or "no post data" in lower
            else "protocol_error"
        )
        return state, None, False, reason


async def request_details(
    record,
    request_file_path="",
    response_file_path="",
    *,
    include_body=True,
    max_body_chars=5000,
):
    metadata = {
        k: v
        for k, v in record.items()
        if k not in {"connection", "post_data"} and not k.startswith("_")
    }
    bodies = []
    for response, path in ((False, request_file_path), (True, response_file_path)):
        name = "Response" if response else "Request"
        if not include_body and not path:
            metadata[name.lower() + "_body_state"] = "not_requested"
            continue
        state, body, encoded, reason = await body_value(record, response)
        metadata[name.lower() + "_body_state"] = state
        if body is None:
            bodies.append(f"{name} body [{state}]: {reason}")
            continue
        if path:
            target = Path(path).expanduser().resolve()
            try:
                await asyncio.to_thread(
                    target.parent.mkdir, parents=True, exist_ok=True
                )
                await asyncio.to_thread(
                    target.write_bytes,
                    base64.b64decode(body) if encoded else body.encode("utf-8"),
                )
            except OSError as exc:
                raise ValueError(
                    f"Cannot save {name.lower()} body to {target}: {exc}"
                ) from exc
            bodies.append(f"{name} body saved to {target}")
        else:
            suffix = (
                f"\n<preview truncated: {len(body)} characters total; use file export for the complete body>"
                if len(body) > max_body_chars
                else ""
            )
            bodies.append(
                f"{name} body{' (base64)' if encoded else ''}: {body[:max_body_chars]}{suffix}"
            )
    return json.dumps(metadata, ensure_ascii=False, indent=2) + "\n" + "\n".join(bodies)
