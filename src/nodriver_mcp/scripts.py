"""Native script execution, bounded protocol resources and complete JSON output."""

import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname
from uuid import uuid4

import nodriver as uc

from .artifacts import artifact_path
from .protocol import json_value

logger = logging.getLogger(__name__)


async def prepare_script_call(arguments):
    """Resolve exactly one source before selecting or launching any browser page."""
    if arguments.get("service_worker_id") and arguments.get("args"):
        raise ValueError("Element args cannot be used with service_worker_id.")
    inline, source_path = arguments.get("function"), arguments.get("source_path", "")
    if (inline is not None) == bool(source_path):
        raise ValueError("Specify exactly one of function or source_path.")
    if source_path:
        local = source_path
        if source_path.startswith("file:"):
            parsed = urlsplit(source_path)
            if parsed.netloc not in {"", "localhost"} and os.name != "nt":
                raise ValueError("source_path must refer to a local file.")
            local = url2pathname(parsed.path)
            if parsed.netloc not in {"", "localhost"}:
                local = "//" + parsed.netloc + local
        path = Path(local).expanduser().resolve()
        try:
            inline = await asyncio.to_thread(path.read_text, encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"Cannot read UTF-8 script from {path}: {exc}") from exc
    arguments["function"] = inline
    arguments["source_path"] = ""


def script_error(error) -> Exception:
    details = json_value(error)
    if isinstance(details, dict) and "text" in details:
        remote = details.get("exception") or {}
        reason = remote.get("description") or details["text"]
        if not remote.get("description") and "value" in remote:
            reason += ": " + json.dumps(remote["value"], ensure_ascii=False)
        url = details.get("url") or "script:" + str(details.get("scriptId", "unknown"))
        return ValueError(
            f"JavaScript exception: {reason}\nat {url}:{details.get('lineNumber', 0) + 1}:{details.get('columnNumber', 0) + 1}"
        )
    return uc.ProtocolException(details)


def script_value(result):
    kind = getattr(result, "type_", None)
    if kind in {"bigint", "symbol", "function"}:
        raise ValueError(
            f"JavaScript {kind} is not JSON serializable; convert it to a string or JSON value."
        )
    raw = getattr(result, "unserializable_value", None)
    if raw is not None:
        return 0 if str(raw) == "-0" else None
    return result.value


async def _execution_command(tab, command, *, synchronous):
    """Keep an in-flight command alive until cancellation can stop its JS."""
    runtime = uc.cdp.runtime
    task = asyncio.create_task(tab.send(command))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        busy = synchronous and not task.done()
        if not synchronous and not task.done():
            # An unresolved Promise need not be executing JS. Terminating an
            # idle isolate can poison its next evaluation. Probe responsiveness
            # before interrupting a continuation that may be running a loop.
            try:
                await asyncio.wait_for(
                    tab.send(
                        runtime.evaluate(
                            "0", silent=True, timeout=runtime.TimeDelta(50)
                        )
                    ),
                    0.1,
                )
            except TimeoutError:
                busy = True
            except Exception:
                pass
        if busy:
            try:
                await asyncio.wait_for(tab.send(runtime.terminate_execution()), 1)
            except Exception:
                logger.debug("Script interruption failed", exc_info=True)
        raise
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _execute_with_arguments(tab, source, objects, group):
    runtime = uc.cdp.runtime
    # Do not await a returned Promise in callFunctionOn: its pending command
    # otherwise cannot distinguish an idle Promise from synchronous busy JS.
    result, error = await _execution_command(
        tab,
        runtime.call_function_on(
            function_declaration=source,
            object_id=objects[0].object_id,
            arguments=[runtime.CallArgument(object_id=o.object_id) for o in objects],
            return_by_value=False,
            await_promise=False,
            user_gesture=True,
            object_group=group,
        ),
        synchronous=True,
    )
    if error or result.object_id is None or result.type_ in {"function", "symbol"}:
        return result, error
    if result.subtype == "promise":
        return await _execution_command(
            tab,
            runtime.await_promise(result.object_id, return_by_value=True),
            synchronous=False,
        )
    return await _execution_command(
        tab,
        runtime.call_function_on(
            "function(){return this}",
            object_id=result.object_id,
            return_by_value=True,
            await_promise=False,
            object_group=group,
        ),
        synchronous=True,
    )


async def execute_script(
    tab,
    source,
    args,
    *,
    format="auto",
    timeout=0,
    context_id=None,
    unique_context_id=None,
    resolve_uid=None,
):
    runtime = uc.cdp.runtime
    group = "nodriver-mcp-script-" + uuid4().hex
    try:
        if args:
            objects = [await resolve_uid(uid, group) for uid in args]
            result, error = await _execute_with_arguments(tab, source, objects, group)
        else:
            expression = source
            if format in {"auto", "function"}:
                candidate = source.rstrip().removesuffix(";")
                if format == "function":
                    expression = "(\n" + candidate + "\n)()"
                else:
                    # Evaluate the source as an argument, outside the wrapper's
                    # lexical scope. IIFEs and comma expressions execute once.
                    wrapped = (
                        "((value) => typeof value === 'function' ? value() : value)((\n"
                        + candidate
                        + "\n))"
                    )
                    # compileScript requires Runtime.enable. A Function
                    # constructor parses without running the source and works
                    # under evaluate's explicit CSP bypass without enabling it.
                    probe = (
                        "(() => {try { (function(){}).constructor("
                        + json.dumps("return (\n" + candidate + "\n);")
                        + "); return true; } catch(error) { if(error.name === 'SyntaxError') return false; throw error; }})()"
                    )
                    parsed, parse_error = await _execution_command(
                        tab,
                        runtime.evaluate(
                            probe,
                            context_id=context_id,
                            unique_context_id=unique_context_id,
                            object_group=group,
                            return_by_value=True,
                            await_promise=False,
                            timeout=runtime.TimeDelta(timeout) if timeout else None,
                            allow_unsafe_eval_blocked_by_csp=True,
                        ),
                        synchronous=False,
                    )
                    if parse_error is not None:
                        raise script_error(parse_error)
                    if parsed.value:
                        expression = wrapped
            result, error = await _execution_command(
                tab,
                runtime.evaluate(
                    expression,
                    context_id=context_id,
                    unique_context_id=unique_context_id,
                    object_group=group,
                    return_by_value=True,
                    await_promise=True,
                    user_gesture=True,
                    timeout=runtime.TimeDelta(timeout) if timeout else None,
                ),
                synchronous=False,
            )
        if error:
            if timeout and error.text.lower() in {
                "execution was terminated",
                "execution timed out",
            }:
                raise TimeoutError(f"Script execution exceeded {timeout}ms.")
            raise script_error(error)
        return script_value(result)
    except uc.ProtocolException as exc:
        if timeout and any(
            text in str(exc).lower()
            for text in ("execution was terminated", "execution timed out")
        ):
            raise TimeoutError(f"Script execution exceeded {timeout}ms.") from exc
        raise
    finally:
        try:
            await asyncio.wait_for(tab.send(runtime.release_object_group(group)), 0.5)
        except Exception:
            logger.debug("Script object-group cleanup failed", exc_info=True)


async def script_result(
    value, file_path: str = "", max_output_chars: int = 20000
) -> str:
    encoded = await asyncio.to_thread(
        json.dumps, json_value(value), ensure_ascii=False, allow_nan=False
    )
    if not file_path and len(encoded) <= max_output_chars:
        return "```json\n" + encoded + "\n```\nStatus: completed"
    path = (
        Path(file_path).expanduser().resolve()
        if file_path
        else artifact_path("scripts", ".json")
    )
    try:
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, encoded, encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Cannot save script output to {path}: {exc}") from exc
    truncated = len(encoded) > max_output_chars
    label = "text" if truncated else "json"
    return (
        f"Output file: {json.dumps(str(path), ensure_ascii=False)}\nCharacters: {len(encoded)}\n"
        + (
            "Preview truncated; the file contains the complete JSON result.\n"
            if truncated
            else ""
        )
        + f"```{label}\n{encoded[:max_output_chars]}\n```\nStatus: completed"
    )
