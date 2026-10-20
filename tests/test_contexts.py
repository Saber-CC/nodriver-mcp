import asyncio
import collections
import gc
import unittest
import weakref
from types import SimpleNamespace as NS

import anyio
from nodriver import cdp

from nodriver_mcp.contexts import FrameContexts


def document(backend):
    return NS(
        node_type=9,
        backend_node_id=cdp.dom.BackendNodeId(backend),
        children=[],
        shadow_roots=None,
        content_document=None,
    )


class Connection:
    __hash__ = None

    def __init__(self):
        self.handlers = collections.defaultdict(list)
        self.commands = []
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.groups = set()
        self.child = document(30)
        self.object_id = "1234.7.1"
        self.checked_object_id = "1234.7.2"
        self.isolate = "4d2"
        self.current_backend = 30

    def add_handler(self, event, callback):
        if callback not in self.handlers[event]:
            self.handlers[event].append(callback)

    def remove_handler(self, event, callback):
        self.handlers[event].remove(callback)

    async def send(self, command):
        packet = next(command)
        method, params = packet["method"], packet.get("params", {})
        self.commands.append(method)
        if method == "DOM.getFrameOwner":
            return cdp.dom.BackendNodeId(20), None
        if method == "DOM.describeNode":
            if "objectId" in params:
                return NS(backend_node_id=cdp.dom.BackendNodeId(self.current_backend))
            return NS(content_document=self.child)
        if method == "DOM.resolveNode":
            self.groups.add(params["objectGroup"])
            self.entered.set()
            if self.block:
                await self.release.wait()
            return cdp.runtime.RemoteObject(
                type_="object", object_id=cdp.runtime.RemoteObjectId(self.object_id)
            )
        if method == "Runtime.getIsolateId":
            return self.isolate
        if method == "Runtime.evaluate":
            return cdp.runtime.RemoteObject(
                type_="object",
                object_id=cdp.runtime.RemoteObjectId(self.checked_object_id),
            ), None
        if method == "Runtime.releaseObjectGroup":
            await asyncio.sleep(0.01)
            self.groups.discard(params["objectGroup"])
        if method == "Runtime.enable":
            context = cdp.runtime.ExecutionContextDescription(
                cdp.runtime.ExecutionContextId(7),
                "http://local.test",
                "",
                "world-A",
                {"frameId": "child", "isDefault": True},
            )
            for callback in list(self.handlers[cdp.runtime.ExecutionContextCreated]):
                callback(cdp.runtime.ExecutionContextCreated(context))


class FrameContextTests(unittest.IsolatedAsyncioTestCase):
    def assert_clean(self, connection):
        self.assertFalse(connection.groups)
        self.assertNotIn("Runtime.addBinding", connection.commands)
        self.assertNotIn("Runtime.removeBinding", connection.commands)
        self.assertFalse(connection.handlers[cdp.runtime.BindingCalled])
        self.assertIn("Runtime.releaseObjectGroup", connection.commands)

    async def test_lookup_never_enables_runtime_and_validates_document_identity(
        self,
    ):
        contexts, connection = FrameContexts(), Connection()
        contexts.watch(connection)
        contexts.watch(connection)
        self.assertEqual(connection.commands, [])
        self.assertEqual(
            len(connection.handlers[cdp.runtime.ExecutionContextCreated]), 1
        )
        self.assertEqual(
            await contexts.main_world(connection, "child"), {"context_id": 7}
        )
        self.assertNotIn("Runtime.enable", connection.commands)
        self.assertNotIn("Runtime.disable", connection.commands)
        self.assert_clean(connection)
        self.assertEqual(
            await contexts.main_world(connection, "child"), {"context_id": 7}
        )
        self.assert_clean(connection)

    async def test_lookup_preserves_explicit_runtime_ownership_and_unique_worlds(self):
        contexts, connection = FrameContexts(), Connection()
        contexts.watch(connection)
        await connection.send(cdp.runtime.enable())
        contexts.request_runtime(connection, True)
        before = list(connection.commands)
        self.assertEqual(
            await contexts.main_world(connection, "child"),
            {"unique_context_id": "world-A"},
        )
        self.assertEqual(connection.commands, before)
        self.assertTrue(contexts.state(connection)["runtime_requested"])
        contexts.request_runtime(connection, False)
        self.assertFalse(contexts.state(connection)["contexts"])
        self.assertEqual(
            await contexts.main_world(connection, "child"), {"context_id": 7}
        )
        self.assert_clean(connection)

    async def test_cancellation_during_document_resolution_releases_object_group(
        self,
    ):
        contexts, connection = FrameContexts(), Connection()
        connection.block = True
        task = asyncio.create_task(contexts.main_world(connection, "child"))
        await asyncio.wait_for(connection.entered.wait(), 1)
        task.cancel()
        connection.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_clean(connection)
        self.assertNotIn("Runtime.enable", connection.commands)
        connection.block = False
        self.assertEqual(
            await contexts.main_world(connection, "child"), {"context_id": 7}
        )

    async def test_anyio_cancellation_releases_frame_handles(self):
        contexts, connection = FrameContexts(), Connection()
        connection.block = True
        scopes = []

        async def request():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await contexts.main_world(connection, "child")

        task = asyncio.create_task(request())
        try:
            await asyncio.wait_for(connection.entered.wait(), 1)
            scopes[0].cancel()
            await asyncio.sleep(0.02)
            connection.release.set()
            await asyncio.wait_for(task, 1)
            self.assert_clean(connection)
            self.assertNotIn("Runtime.enable", connection.commands)
        finally:
            connection.release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_unknown_or_changed_chrome_objects_never_enable_runtime_as_a_fallback(
        self,
    ):
        for changes in (
            {"object_id": "opaque"},
            {"isolate": "abcd"},
            {"current_backend": 31},
            {"checked_object_id": "2345.7.2"},
        ):
            with self.subTest(changes=changes):
                contexts, connection = FrameContexts(), Connection()
                for key, value in changes.items():
                    setattr(connection, key, value)
                with self.assertRaises(ValueError):
                    await contexts.main_world(connection, "child")
                self.assert_clean(connection)
                self.assertNotIn("Runtime.enable", connection.commands)

    async def test_signed_object_identifiers_match_unsigned_native_isolate_ids(self):
        contexts, connection = FrameContexts(), Connection()
        connection.object_id = "-1.7.1"
        connection.checked_object_id = "-1.7.2"
        connection.isolate = "ffffffffffffffff"
        self.assertEqual(
            await contexts.main_world(connection, "child"), {"context_id": 7}
        )
        self.assert_clean(connection)

    async def test_destroyed_events_match_unique_world_and_watchers_do_not_retain_pages(
        self,
    ):
        contexts, connection = FrameContexts(), Connection()
        contexts.watch(connection)
        external = lambda event: None
        connection.add_handler(cdp.runtime.ConsoleAPICalled, external)
        await connection.send(cdp.runtime.enable())
        for callback in connection.handlers[cdp.runtime.ExecutionContextDestroyed]:
            callback(
                NS(execution_context_id=7, execution_context_unique_id="world-old")
            )
        self.assertIn("child", contexts.state(connection)["contexts"])
        for callback in connection.handlers[cdp.runtime.ExecutionContextDestroyed]:
            callback(NS(execution_context_id=7, execution_context_unique_id="world-A"))
        self.assertFalse(contexts.state(connection)["contexts"])
        reference = weakref.ref(connection)
        del callback, connection
        gc.collect()
        self.assertIsNone(reference())
        self.assertFalse(contexts.watchers)
