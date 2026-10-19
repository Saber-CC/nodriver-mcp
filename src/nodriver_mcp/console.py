"""Explicit console/exception collection with native stack metadata."""

import json

from nodriver import cdp

from .protocol import json_value


def stack_location(stack):
    frames = (stack or {}).get("callFrames", [])
    if not frames:
        return None
    frame = frames[0]
    return {
        "url": frame.get("url", ""),
        "script_id": frame.get("scriptId"),
        "line_number": frame.get("lineNumber", 0),
        "column_number": frame.get("columnNumber", 0),
    }


def format_stack(stack, limit=50):
    lines = []
    while stack and len(lines) < limit:
        if stack.get("description"):
            lines.append("  --- " + stack["description"] + " ---")
        for frame in stack.get("callFrames", []):
            url = frame.get("url") or "script:" + str(frame.get("scriptId", "unknown"))
            lines.append(
                f"  at {frame.get('functionName') or '<anonymous>'} "
                f"({url}:{frame.get('lineNumber', 0) + 1}:{frame.get('columnNumber', 0) + 1})"
            )
            if len(lines) >= limit:
                break
        stack = stack.get("parent")
    return "\n".join(lines)


def format_message(message, *, include_stack_traces=True, detailed=True):
    lines = [
        f"[{message['type']}] {message['text']} (timestamp: {message['timestamp']})"
    ]
    if detailed:
        metadata = {
            key: message[key]
            for key in (
                "source",
                "location",
                "execution_context_id",
                "exception_id",
                "revoked",
                "revocation_reason",
            )
            if message.get(key) is not None
        }
        if metadata:
            lines.append(json.dumps(metadata, ensure_ascii=False, indent=2))
    if include_stack_traces:
        stack = format_stack(message.get("stack_trace"))
        if stack:
            lines.append("Stack trace:\n" + stack)
    return "\n".join(lines)


class ConsoleCollector:
    def __init__(self, connection, collect, records):
        self.connection, self.collect, self.records = connection, collect, records
        self.callbacks = [
            (cdp.runtime.ConsoleAPICalled, self.console),
            (cdp.runtime.ExceptionThrown, self.thrown),
            (cdp.runtime.ExceptionRevoked, self.revoked),
        ]

    async def enable(self):
        for event, callback in self.callbacks:
            self.connection.add_handler(event, callback)
        try:
            await self.connection.send(cdp.runtime.enable())
        except BaseException:
            self.remove()
            raise

    def remove(self):
        for event, callback in self.callbacks:
            self.connection.remove_handler(event, callback)

    def console(self, event):
        parts = [
            str(a.value if a.value is not None else a.description or a.type_)
            for a in event.args
        ]
        stack = json_value(event.stack_trace)
        self.collect(
            {
                "source": "console",
                "type": event.type_,
                "text": " ".join(parts),
                "timestamp": float(event.timestamp),
                "stack_trace": stack,
                "location": stack_location(stack),
                "execution_context_id": int(event.execution_context_id),
            }
        )

    def thrown(self, event):
        details = event.exception_details
        remote = details.exception
        description = remote.description if remote else None
        if not description and remote and remote.value is not None:
            description = json.dumps(json_value(remote.value), ensure_ascii=False)
        text = (
            description or details.text or "Uncaught JavaScript exception"
        ).splitlines()[0]
        if details.text and not text.startswith(details.text):
            text = details.text + ": " + text
        stack = json_value(details.stack_trace)
        location = stack_location(stack) or {}
        location.update(
            line_number=details.line_number, column_number=details.column_number
        )
        if details.url:
            location["url"] = details.url
        if details.script_id is not None:
            location["script_id"] = str(details.script_id)
        self.collect(
            {
                "source": "exception",
                "type": "error",
                "text": text,
                "timestamp": float(event.timestamp),
                "stack_trace": stack,
                "location": location,
                "exception_id": details.exception_id,
                "execution_context_id": int(details.execution_context_id)
                if details.execution_context_id is not None
                else None,
            }
        )

    def revoked(self, event):
        for record in reversed(self.records()):
            if (
                record.get("connection") is self.connection
                and record.get("exception_id") == event.exception_id
            ):
                record.update(revoked=True, revocation_reason=event.reason)
                break
