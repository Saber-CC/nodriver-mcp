import asyncio
import collections
import gc
import unittest
import weakref
from types import SimpleNamespace as NS

import anyio
from nodriver import cdp

from nodriver_mcp.contexts import FrameContexts
from nodriver_mcp.scripts import execute_script


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
        self.object_id = "opaque-document-handle"

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
            return NS(content_document=self.child)
        if method == "DOM.resolveNode":
            self.groups.add(params["objectGroup"])
            self.entered.set()
            if self.block:
                await self.release.wait()
            return cdp.runtime.RemoteObject(
                type_="object",
                object_id=cdp.runtime.RemoteObjectId(self.object_id)
                if self.object_id is not None
                else None,
            )
        if method == "Runtime.callFunctionOn" and not params.get("arguments"):
            return cdp.runtime.RemoteObject(
                type_="function",
                description="function eval() { [native code] }",
                object_id=cdp.runtime.RemoteObjectId("opaque-native-eval-handle"),
            ), None
        if method in {"Runtime.evaluate", "Runtime.callFunctionOn"}:
            return cdp.runtime.RemoteObject(type_="number", value=42), None
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

    async def test_lookup_uses_opaque_document_reference_in_callers_object_group(
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
            await contexts.main_world(connection, "child", object_group="caller"),
            {"document_object_id": "opaque-document-handle"},
        )
        self.assertEqual(connection.groups, {"caller"})
        self.assertEqual(
            connection.commands,
            ["DOM.getFrameOwner", "DOM.describeNode", "DOM.resolveNode"],
        )
        self.assertNotIn("Runtime.enable", connection.commands)
        self.assertNotIn("Runtime.disable", connection.commands)
        self.assertNotIn("Runtime.releaseObjectGroup", connection.commands)
        await connection.send(cdp.runtime.release_object_group("caller"))
        self.assert_clean(connection)
        self.assertEqual(
            await execute_script(
                connection,
                "42",
                None,
                format="script",
                resolve_context=lambda group: contexts.main_world(
                    connection, "child", object_group=group
                ),
            ),
            42,
        )
        self.assert_clean(connection)

    async def test_lookup_preserves_explicit_runtime_ownership_and_unique_worlds(self):
        contexts, connection = FrameContexts(), Connection()
        contexts.watch(connection)
        await connection.send(cdp.runtime.enable())
        contexts.request_runtime(connection, True)
        before = list(connection.commands)
        self.assertEqual(
            await contexts.main_world(connection, "child", object_group="caller"),
            {"unique_context_id": "world-A"},
        )
        self.assertEqual(connection.commands, before)
        self.assertTrue(contexts.state(connection)["runtime_requested"])
        contexts.request_runtime(connection, False)
        self.assertFalse(contexts.state(connection)["contexts"])
        self.assertEqual(
            await contexts.main_world(connection, "child", object_group="caller"),
            {"document_object_id": "opaque-document-handle"},
        )
        await connection.send(cdp.runtime.release_object_group("caller"))
        self.assert_clean(connection)

    async def test_cancellation_during_document_resolution_releases_object_group(
        self,
    ):
        contexts, connection = FrameContexts(), Connection()
        connection.block = True

        async def run():
            return await execute_script(
                connection,
                "42",
                None,
                format="script",
                resolve_context=lambda group: contexts.main_world(
                    connection, "child", object_group=group
                ),
            )

        task = asyncio.create_task(run())
        await asyncio.wait_for(connection.entered.wait(), 1)
        task.cancel()
        connection.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_clean(connection)
        self.assertNotIn("Runtime.enable", connection.commands)
        connection.block = False
        self.assertEqual(
            await run(),
            42,
        )
        self.assert_clean(connection)

    async def test_anyio_cancellation_releases_frame_handles(self):
        contexts, connection = FrameContexts(), Connection()
        connection.block = True
        scopes = []

        async def request():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await execute_script(
                    connection,
                    "42",
                    None,
                    format="script",
                    resolve_context=lambda group: contexts.main_world(
                        connection, "child", object_group=group
                    ),
                )

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

    async def test_missing_documents_and_handles_fail_without_enabling_runtime(
        self,
    ):
        for changes in (
            {"child": None},
            {"object_id": None},
        ):
            with self.subTest(changes=changes):
                contexts, connection = FrameContexts(), Connection()
                for key, value in changes.items():
                    setattr(connection, key, value)
                with self.assertRaises(ValueError):
                    await execute_script(
                        connection,
                        "42",
                        None,
                        format="script",
                        resolve_context=lambda group: contexts.main_world(
                            connection, "child", object_group=group
                        ),
                    )
                self.assert_clean(connection)
                self.assertNotIn("Runtime.enable", connection.commands)

    async def test_document_is_retained_through_execution_before_group_cleanup(self):
        contexts, connection = FrameContexts(), Connection()
        send = connection.send

        async def check(command):
            if command.gi_code.co_name == "call_function_on":
                self.assertTrue(connection.groups)
                self.assertNotIn("Runtime.releaseObjectGroup", connection.commands)
            return await send(command)

        connection.send = check
        self.assertEqual(
            await execute_script(
                connection,
                "42",
                None,
                format="script",
                resolve_context=lambda group: contexts.main_world(
                    connection, "child", object_group=group
                ),
            ),
            42,
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
