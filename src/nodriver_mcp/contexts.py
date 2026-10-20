"""Observe default frame worlds without enabling Runtime during page startup."""

import asyncio
import logging
import weakref

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
                self.reset(connection)
                try:
                    # Leave buffered logs available for the first explicit
                    # console collector, including after cancelled discovery.
                    await asyncio.wait_for(connection.send(cdp.runtime.disable()), 0.5)
                except Exception:
                    logger.debug("Temporary Runtime cleanup failed", exc_info=True)

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
