"""Resolve default frame worlds without implicitly enabling Runtime events."""

import logging
import weakref
from uuid import uuid4

import anyio
from nodriver import cdp

from .scripts import script_error

logger = logging.getLogger(__name__)


class FrameContexts:
    def __init__(self):
        self.watchers = {}

    def state(self, connection):
        state = self.watchers.get(id(connection))
        return (
            state if state is not None and state["connection"]() is connection else None
        )

    def watch(self, connection):
        existing = self.state(connection)
        if existing is not None:
            return existing
        contexts = {}

        def created(event):
            context = event.context
            aux = context.aux_data or {}
            if aux.get("isDefault") and aux.get("frameId"):
                contexts[str(aux["frameId"])] = context

        def destroyed(event):
            for frame_id, context in list(contexts.items()):
                if (
                    context.id_ == event.execution_context_id
                    and context.unique_id == event.execution_context_unique_id
                ):
                    contexts.pop(frame_id, None)

        def cleared(_event):
            contexts.clear()

        callbacks = [
            (cdp.runtime.ExecutionContextCreated, created),
            (cdp.runtime.ExecutionContextDestroyed, destroyed),
            (cdp.runtime.ExecutionContextsCleared, cleared),
        ]
        for event, callback in callbacks:
            connection.add_handler(event, callback)
        key = id(connection)

        def collected(reference):
            current = self.watchers.get(key)
            if current is not None and current["connection"] is reference:
                self.watchers.pop(key, None)

        state = {
            "contexts": contexts,
            "callbacks": callbacks,
            "connection": weakref.ref(connection, collected),
            "runtime_requested": False,
        }
        self.watchers[key] = state
        return state

    def has_runtime_consumers(self, connection):
        state = self.state(connection)
        if state and state["runtime_requested"]:
            return True
        passive = {callback for _, callback in state["callbacks"]} if state else set()
        return any(
            callback not in passive and not getattr(callback, "_runtime_passive", False)
            for event, callbacks in connection.handlers.items()
            if event.__module__ == cdp.runtime.__name__
            for callback in callbacks
        )

    def reset(self, connection):
        state = self.state(connection)
        if state:
            state["contexts"].clear()

    def request_runtime(self, connection, enabled):
        self.watch(connection)["runtime_requested"] = enabled
        if not enabled:
            self.reset(connection)

    async def restore_runtime(self, connection):
        """Reconcile a possibly applied command without discarding live worlds."""
        state = self.state(connection)
        if state is None:
            return
        enabled = self.has_runtime_consumers(connection)
        with anyio.move_on_after(0.5, shield=True):
            try:
                await connection.send(
                    cdp.runtime.enable() if enabled else cdp.runtime.disable()
                )
                if not enabled and self.state(connection) is state:
                    state["contexts"].clear()
            except Exception:
                logger.debug("Runtime state recovery failed", exc_info=True)

    async def main_world(self, connection, frame_id):
        state = self.watch(connection)
        context = state["contexts"].get(frame_id)
        if context is not None:
            return {"unique_context_id": context.unique_id}

        owner, _ = await connection.send(
            cdp.dom.get_frame_owner(cdp.page.FrameId(frame_id))
        )
        node = await connection.send(
            cdp.dom.describe_node(backend_node_id=owner, depth=1, pierce=True)
        )
        if node.content_document is None:
            raise ValueError(
                "This frame needs a separate connection; refresh list_frames."
            )
        backend_id = node.content_document.backend_node_id
        group = "nodriver-mcp-context-" + uuid4().hex
        try:
            document = await connection.send(
                cdp.dom.resolve_node(backend_node_id=backend_id, object_group=group)
            )
            # Chrome's V8 RemoteObjectId currently carries isolate.context.object.
            # This format is opaque in CDP, so validate it against both the live
            # isolate and the selected Document; never fall back to Runtime.enable
            # or install a binding in the page if Chrome changes the format.
            parts = str(document.object_id).split(".")
            if (
                len(parts) != 3
                or not parts[0].removeprefix("-").isdecimal()
                or not all(part.isdecimal() for part in parts[1:])
                or int(parts[1]) <= 0
            ):
                raise ValueError(
                    "Chrome returned an unsupported frame object identifier; cannot resolve its execution context without enabling Runtime."
                )
            isolate = await connection.send(cdp.runtime.get_isolate_id())
            if int(parts[0]) & ((1 << 64) - 1) != int(isolate, 16):
                raise ValueError(
                    "Frame execution context changed during discovery; refresh frames and retry."
                )
            context_id = cdp.runtime.ExecutionContextId(int(parts[1]))
            checked, error = await connection.send(
                cdp.runtime.evaluate(
                    "document", context_id=context_id, object_group=group
                )
            )
            if error:
                raise script_error(error)
            if str(checked.object_id).rsplit(".", 1)[0] != ".".join(parts[:2]):
                raise ValueError(
                    "Frame execution context changed during discovery; refresh frames and retry."
                )
            current = await connection.send(
                cdp.dom.describe_node(object_id=checked.object_id, depth=0)
            )
            if current.backend_node_id != backend_id:
                raise ValueError(
                    "Execution context does not belong to the selected frame; refresh frames and retry."
                )
            return {"context_id": context_id}
        finally:
            with anyio.move_on_after(0.5, shield=True):
                try:
                    await connection.send(cdp.runtime.release_object_group(group))
                except Exception:
                    logger.debug("Frame context object cleanup failed", exc_info=True)

    def forget(self, connection):
        state = self.watchers.pop(id(connection), None)
        if state:
            for event, callback in state["callbacks"]:
                if callback in connection.handlers.get(event, []):
                    connection.remove_handler(event, callback)

    def clear(self):
        for state in list(self.watchers.values()):
            connection = state["connection"]()
            if connection is not None:
                self.forget(connection)
        self.watchers.clear()
