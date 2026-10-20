"""Observe default frame worlds without enabling Runtime during page startup."""

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
            callback not in passive
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
        try:
            # A previous console/CDP consumer may already own Runtime. Repeated
            # enable is harmless, and replays contexts if it was disabled.
            await connection.send(cdp.runtime.enable())
            context = state["contexts"].get(frame_id)
            if context is None:
                raise ValueError(
                    "Frame main execution context is unavailable; refresh frames."
                )
            return context.unique_id
        finally:
            if not self.has_runtime_consumers(connection):
                # Leave buffered logs available for the first explicit console
                # collector, including after cancelled discovery. Clear worlds
                # only after Chrome confirms that Runtime is disabled.
                await self.restore_runtime(connection)

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
