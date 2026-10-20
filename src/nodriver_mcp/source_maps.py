"""Opt-in source-map v3 symbolization through the browser's own resource loader."""

import asyncio
import base64
import copy
import json
import posixpath
from bisect import bisect_right
from collections import OrderedDict, defaultdict
from urllib.parse import unquote_to_bytes, urljoin, urlsplit
from uuid import uuid4

from nodriver import ProtocolException, cdp

from .protocol import json_value

BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


def resolve_url(base, path):
    if urlsplit(path).scheme:
        return path
    parsed = urlsplit(base)
    if parsed.scheme and parsed.scheme not in {"http", "https", "file", "ftp"}:
        joined = posixpath.normpath(
            path
            if path.startswith("/")
            else posixpath.join(posixpath.dirname(parsed.path), path)
        )
        return parsed.scheme + "://" + parsed.netloc + "/" + joined.lstrip("/")
    return urljoin(base, path)


def vlq(segment):
    result, value, shift = [], 0, 0
    for char in segment:
        digit = BASE64.find(char)
        if digit < 0 or shift > 60:
            raise ValueError("Invalid source-map VLQ segment.")
        value |= (digit & 31) << shift
        if digit & 32:
            shift += 5
        else:
            result.append(-(value >> 1) if value & 1 else value >> 1)
            value, shift = 0, 0
    if shift:
        raise ValueError("Incomplete source-map VLQ segment.")
    return result


class SourceMap:
    def __init__(self, data, url):
        self.rows = defaultdict(list)
        self.weight = 0
        self.parse(data, url, 0, 0)
        self.columns = {}
        for line, entries in self.rows.items():
            entries.sort(key=lambda entry: entry[0])
            self.columns[line] = [entry[0] for entry in entries]

    def parse(self, data, url, offset_line, offset_column):
        if not isinstance(data, dict) or data.get("version") != 3:
            raise ValueError("Only source-map version 3 is supported.")
        url = data.get("_map_url", url)
        if "sections" in data:
            last = (-1, -1)
            for section in data["sections"]:
                line, column = section["offset"]["line"], section["offset"]["column"]
                if line < 0 or column < 0 or (line, column) <= last:
                    raise ValueError("Invalid indexed source-map offsets.")
                last = (line, column)
                absolute_line = offset_line + line
                absolute_column = offset_column + column if line == 0 else column
                self.rows[absolute_line].append((absolute_column, None))
                self.parse(section["map"], url, absolute_line, absolute_column)
            return
        sources, names = data.get("sources", []), data.get("names", [])
        if (
            not isinstance(sources, list)
            or not isinstance(names, list)
            or any(not isinstance(s, str) for s in sources + names)
        ):
            raise ValueError("Source-map sources/names must be string arrays.")
        if not isinstance(data.get("mappings", ""), str):
            raise ValueError("Source-map mappings must be a string.")
        content = data.get("sourcesContent", []) or []
        if not isinstance(content, list) or any(
            c is not None and not isinstance(c, str) for c in content
        ):
            raise ValueError("Invalid source-map sourcesContent.")
        root = data.get("sourceRoot", "")
        source_base = resolve_url(url, root.rstrip("/") + "/") if root else url
        source_urls = [resolve_url(source_base, source) for source in sources]
        content_lines = [
            text.splitlines() if text is not None else [] for text in content
        ]
        self.weight += sum(len(text or "") * 4 for text in content)
        source_index = original_line = original_column = name_index = 0
        for line, encoded in enumerate(data.get("mappings", "").split(";")):
            generated_column = 0
            for segment in encoded.split(","):
                if not segment:
                    continue
                values = vlq(segment)
                if len(values) not in {1, 4, 5}:
                    raise ValueError("Invalid source-map segment length.")
                if values[0] < 0:
                    raise ValueError("Source-map generated columns must be ordered.")
                generated_column += values[0]
                location = None
                if len(values) > 1:
                    source_index += values[1]
                    original_line += values[2]
                    original_column += values[3]
                    if (
                        not 0 <= source_index < len(sources)
                        or min(original_line, original_column) < 0
                    ):
                        raise ValueError("Invalid source-map original position.")
                    location = {
                        "url": source_urls[source_index],
                        "lineNumber": original_line,
                        "columnNumber": original_column,
                    }
                    if len(values) == 5:
                        name_index += values[4]
                        if not 0 <= name_index < len(names):
                            raise ValueError("Invalid source-map name index.")
                        location["functionName"] = names[name_index]
                    if (
                        source_index < len(content)
                        and content[source_index] is not None
                    ):
                        lines = content_lines[source_index]
                        if original_line < len(lines):
                            location["sourceLine"] = lines[original_line][:500]
                    if source_index in data.get(
                        "ignoreList", data.get("x_google_ignoreList", [])
                    ):
                        location["ignored"] = True
                self.weight += 200
                self.rows[offset_line + line].append(
                    (generated_column + (offset_column if line == 0 else 0), location)
                )

    def find(self, line, column):
        index = bisect_right(self.columns.get(line, []), column) - 1
        return self.rows[line][index][1] if index >= 0 else None


class SourceMaps:
    def __init__(self, connection):
        self.connection = connection
        self.active = self.ever_enabled = self.watching = False
        self.scripts, self.cache = OrderedDict(), OrderedDict()
        self.contexts = {}
        self.callbacks = [
            (cdp.debugger.ScriptParsed, self.parsed),
            (cdp.debugger.ScriptFailedToParse, self.parsed),
            (cdp.runtime.ExecutionContextCreated, self.context_created),
            (cdp.runtime.ExecutionContextDestroyed, self.context_destroyed),
            (cdp.runtime.ExecutionContextsCleared, self.contexts_cleared),
        ]
        self.lock = asyncio.Lock()

    def parsed(self, event):
        aux = event.execution_context_aux_data or {}
        key = str(event.script_id)
        script = {
            "id": str(event.script_id),
            "context_id": int(event.execution_context_id),
            "context_unique_id": self.contexts.get(int(event.execution_context_id)),
            "hash": event.hash_,
            "url": event.url,
            "map_url": event.source_map_url,
            "frame_id": aux.get("frameId"),
            "start_line": event.start_line,
            "start_column": event.start_column,
        }
        previous = self.scripts.get(key)
        if previous is not None:
            if not previous.get("retired") and all(
                previous.get(k) == v for k, v in script.items()
            ):
                return
            previous["retired"] = True
            # Old messages retain this catalog, including lazily resolved async
            # parents. Replacing an ID must not change their script ownership.
            self.scripts = self.scripts.copy()
        self.scripts[key] = dict(script, identity=uuid4().hex)
        while len(self.scripts) > 10000:
            self.scripts.popitem(last=False)

    def context_created(self, event):
        context = event.context
        context_id = int(context.id_)
        self.contexts[context_id] = context.unique_id
        # Debugger may describe existing scripts before Runtime replays their
        # contexts. Complete that identity before the next Debugger enable.
        for script in self.scripts.values():
            if (
                script.get("context_id") == context_id
                and script.get("context_unique_id") is None
                and not script.get("retired")
            ):
                script["context_unique_id"] = context.unique_id

    def context_destroyed(self, event):
        context_id = int(event.execution_context_id)
        unique_id = event.execution_context_unique_id
        if self.contexts.get(context_id) == unique_id:
            self.contexts.pop(context_id, None)
        for script in self.scripts.values():
            if script.get("context_id") == context_id and (
                not script.get("context_unique_id")
                or script["context_unique_id"] == unique_id
            ):
                script["retired"] = True

    def contexts_cleared(self, event):
        for script in self.scripts.values():
            script["retired"] = True
        self.contexts.clear()
        self.scripts = OrderedDict()

    def bind(self, message):
        """Keep generated frames tied to the scripts observed with this log."""
        message["_source_maps"] = self
        message["_source_catalog"] = self.scripts
        scripts = {}
        current = message.get("stack_trace")
        for _ in range(8):
            if not current:
                break
            for frame in current.get("callFrames", [])[:50]:
                key = str(frame.get("scriptId"))
                scripts[key] = self.scripts.get(key)
            current = current.get("parent")
        if message.get("location"):
            key = str(message["location"].get("script_id"))
            scripts[key] = self.scripts.get(key)
        message["_source_scripts"] = scripts

    async def enable(self):
        if self.active:
            return
        self.watching = True
        for event, callback in self.callbacks:
            self.connection.add_handler(event, callback)
        try:
            await self.connection.send(cdp.debugger.enable())
            self.active = True
            await self.connection.send(cdp.debugger.set_skip_all_pauses(True))
            await self.connection.send(cdp.debugger.set_async_call_stack_depth(32))
            self.ever_enabled = True
        except BaseException:
            try:
                await asyncio.wait_for(self.disable(), 0.5)
            except Exception:
                self.retire()
            raise

    def retire(self):
        self.active = False
        if not self.watching:
            return
        self.watching = False
        for event, callback in self.callbacks:
            if callback in self.connection.handlers.get(event, []):
                self.connection.remove_handler(event, callback)

    async def disable(self):
        enabled = self.active
        self.retire()
        if enabled and not any(
            callbacks
            for event, callbacks in self.connection.handlers.items()
            if event.__module__ == cdp.debugger.__name__
        ):
            await self.connection.send(cdp.debugger.disable())

    async def fetch(self, url, script):
        if url.startswith("data:"):
            metadata, separator, payload = url.partition(",")
            if not separator:
                raise ValueError("Malformed inline source map.")
            raw = unquote_to_bytes(payload)
            raw = base64.b64decode(raw, validate=True) if ";base64" in metadata else raw
        else:
            if not self.active or script.get("retired"):
                raise ValueError("Source-map execution context is retired or disabled.")
            result = await self.connection.send(
                cdp.network.load_network_resource(
                    url,
                    cdp.network.LoadNetworkResourceOptions(
                        disable_cache=False, include_credentials=True
                    ),
                    frame_id=cdp.page.FrameId(script["frame_id"])
                    if script.get("frame_id")
                    else None,
                )
            )
            if result.stream is None:
                raise ValueError(
                    f"Cannot load source map (HTTP {result.http_status_code}, {result.net_error_name})."
                )
            chunks, length = [], 0
            try:
                if not result.success or (result.http_status_code or 0) >= 400:
                    raise ValueError(
                        f"Cannot load source map (HTTP {result.http_status_code}, {result.net_error_name})."
                    )
                while True:
                    if not self.active or script.get("retired"):
                        raise ValueError(
                            "Source-map execution context is retired or disabled."
                        )
                    encoded, text, eof = await self.connection.send(
                        cdp.io.read(result.stream, size=65536)
                    )
                    chunk = base64.b64decode(text) if encoded else text.encode("utf-8")
                    chunks.append(chunk)
                    length += len(chunk)
                    if length > 20 * 1024 * 1024:
                        raise ValueError("Source map exceeds 20 MiB.")
                    if eof:
                        break
            finally:
                if self.active:
                    await asyncio.wait_for(
                        self.connection.send(cdp.io.close(result.stream)), 0.5
                    )
            raw = b"".join(chunks)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("Source map exceeds 20 MiB.")
        return json.loads(raw.decode("utf-8-sig"))

    async def expand(self, data, url, script, depth=0):
        if depth > 8:
            raise ValueError("Indexed source-map nesting exceeds 8 levels.")
        if not isinstance(data, dict):
            raise ValueError("Source map must be a JSON object.")
        data = dict(data, _map_url=url)
        if "sections" in data:
            sections = []
            for section in data["sections"]:
                child_url = (
                    resolve_url(url, section["url"]) if "url" in section else url
                )
                child = (
                    await self.fetch(child_url, script)
                    if "url" in section
                    else section["map"]
                )
                sections.append(
                    {
                        "offset": section["offset"],
                        "map": await self.expand(child, child_url, script, depth + 1),
                    }
                )
            data["sections"] = sections
        return data

    async def map_for(self, script):
        async with self.lock:
            key = script.setdefault("identity", uuid4().hex)
            if key not in self.cache:
                try:
                    url = resolve_url(script["url"], script["map_url"])
                    base = script["url"] if url.startswith("data:") else url
                    data = await self.fetch(url, script)
                    self.cache[key] = await asyncio.to_thread(
                        SourceMap, await self.expand(data, base, script), base
                    )
                except (
                    ValueError,
                    KeyError,
                    TypeError,
                    UnicodeError,
                    ProtocolException,
                ) as exc:
                    self.cache[key] = str(exc)[:500]
                while len(self.cache) > 1 and (
                    len(self.cache) > 128
                    or sum(getattr(v, "weight", 1000) for v in self.cache.values())
                    > 64 * 1024 * 1024
                ):
                    self.cache.popitem(last=False)
            value = self.cache[key]
            if isinstance(value, str):
                raise ValueError(value)
            return value

    async def frame(self, frame, scripts=None, catalog=None):
        key = str(frame.get("scriptId"))
        scripts = scripts if scripts is not None else self.scripts
        script = scripts.get(key) if key in scripts else (catalog or {}).get(key)
        if script is None or not script.get("map_url"):
            return
        if frame.get("url") and script["url"] and frame["url"] != script["url"]:
            frame["sourceMapError"] = (
                "Generated frame does not match the captured script."
            )
            return
        try:
            mapping = await self.map_for(script)
            line = frame.get("lineNumber", 0) - script.get("start_line", 0)
            column = frame.get("columnNumber", 0) - (
                script.get("start_column", 0) if line == 0 else 0
            )
            location = mapping.find(line, column)
            if location:
                frame["originalLocation"] = location
        except (ValueError, KeyError, TypeError) as exc:
            frame["sourceMapError"] = str(exc)[:500]

    async def symbolize(self, message):
        if not self.ever_enabled:
            return message
        result = dict(message)
        scripts = message.get("_source_scripts", self.scripts)
        catalog = message.get("_source_catalog", self.scripts)
        stack = copy.deepcopy(message.get("stack_trace"))
        result["stack_trace"] = stack
        current, visited, errors = stack, set(), []
        for _ in range(8):
            if not current:
                break
            for frame in current.get("callFrames", [])[:50]:
                await self.frame(frame, scripts, catalog)
                if frame.get("sourceMapError"):
                    errors.append(frame["sourceMapError"])
            if "parent" not in current and current.get("parentId"):
                parent = current["parentId"]
                key = json.dumps(parent, sort_keys=True)
                if key in visited or not self.active or catalog is not self.scripts:
                    break
                visited.add(key)
                try:
                    current["parent"] = json_value(
                        await self.connection.send(
                            cdp.debugger.get_stack_trace(
                                cdp.runtime.StackTraceId.from_json(parent)
                            )
                        )
                    )
                except Exception as exc:
                    errors.append("Async stack unavailable: " + str(exc)[:300])
            current = current.get("parent")
        if stack and stack.get("callFrames"):
            result["mapped_location"] = stack["callFrames"][0].get("originalLocation")
        if not result.get("mapped_location") and message.get("location"):
            native = message["location"]
            frame = {
                "scriptId": native.get("script_id"),
                "url": native.get("url", ""),
                "lineNumber": native.get("line_number", 0),
                "columnNumber": native.get("column_number", 0),
            }
            await self.frame(frame, scripts, catalog)
            if frame.get("originalLocation"):
                result["mapped_location"] = frame["originalLocation"]
            if frame.get("sourceMapError"):
                errors.append(frame["sourceMapError"])
        if errors:
            result["source_map_errors"] = list(dict.fromkeys(errors))
        return result


async def symbolize_messages(messages, enabled=True):
    if not enabled:
        return messages

    async def one(message):
        maps = message.get("_source_maps")
        return await maps.symbolize(message) if maps is not None else message

    try:
        return await asyncio.wait_for(asyncio.gather(*(one(m) for m in messages)), 5)
    except TimeoutError:
        return [
            dict(
                m,
                source_map_errors=[
                    "Source map resolution exceeded 5000ms; generated positions retained."
                ],
            )
            for m in messages
        ]


async def symbolize_message(message, enabled=True):
    return (await symbolize_messages([message], enabled))[0]
