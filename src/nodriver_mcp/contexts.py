"""Resolve default frame worlds without implicitly enabling Runtime events."""

import logging
import weakref

import anyio
from nodriver import cdp

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

    async def main_world(self, connection, frame_id, *, object_group):
        """Resolve a world using handles retained by the caller's object group."""
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
        document = await connection.send(
            cdp.dom.resolve_node(
                backend_node_id=node.content_document.backend_node_id,
                object_group=object_group,
            )
        )
        if document.object_id is None:
            raise ValueError("Frame document is unavailable; refresh frames and retry.")
        # RemoteObjectId is opaque and Chrome validates its owning isolate and
        # context during callFunctionOn. Keep it alive until execution finishes;
        # a numeric contextId could be reused by a different renderer meanwhile.
        return {"document_object_id": document.object_id}

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
