import asyncio
import collections
import gc
import unittest
import weakref
from types import SimpleNamespace as NS
from unittest.mock import patch

import anyio
from nodriver import cdp

from nodriver_mcp.contexts import FrameContexts


class Connection:
    __hash__ = None

    def __init__(self):
        self.handlers = collections.defaultdict(list)
        self.commands = []
        self.block = False
        self.entered = asyncio.Event()

    def add_handler(self, event, callback):
        if callback not in self.handlers[event]:
            self.handlers[event].append(callback)

    def remove_handler(self, event, callback):
        self.handlers[event].remove(callback)

    async def send(self, command):
        method = next(command)["method"]
        self.commands.append(method)
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
            self.entered.set()
            if self.block:
                await asyncio.Future()


class FrameContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_passive_watch_and_temporary_lookup_preserve_runtime_ownership(self):
        contexts, connection = FrameContexts(), Connection()
        contexts.watch(connection)
        contexts.watch(connection)
        self.assertEqual(connection.commands, [])
        self.assertEqual(
            len(connection.handlers[cdp.runtime.ExecutionContextCreated]), 1
        )
        self.assertEqual(await contexts.main_world(connection, "child"), "world-A")
        self.assertEqual(connection.commands, ["Runtime.enable", "Runtime.disable"])
        self.assertFalse(contexts.state(connection)["contexts"])
        external = lambda event: None
        connection.add_handler(cdp.runtime.ConsoleAPICalled, external)
        self.assertEqual(await contexts.main_world(connection, "child"), "world-A")
        self.assertEqual(connection.commands[-1], "Runtime.enable")
        contexts.forget(connection)
        self.assertEqual(connection.handlers[cdp.runtime.ConsoleAPICalled], [external])
        self.assertFalse(connection.handlers[cdp.runtime.ExecutionContextCreated])

        connection.remove_handler(cdp.runtime.ConsoleAPICalled, external)
        contexts.request_runtime(connection, True)
        self.assertEqual(await contexts.main_world(connection, "child"), "world-A")
        self.assertEqual(connection.commands[-1], "Runtime.enable")
        contexts.request_runtime(connection, False)
        self.assertFalse(contexts.state(connection)["contexts"])
        self.assertEqual(await contexts.main_world(connection, "child"), "world-A")
        self.assertEqual(connection.commands[-1], "Runtime.disable")

    async def test_canceled_lookup_closes_runtime_and_can_replay_contexts_on_retry(
        self,
    ):
        contexts, connection = FrameContexts(), Connection()
        connection.block = True
        task = asyncio.create_task(contexts.main_world(connection, "child"))
        await asyncio.wait_for(connection.entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(connection.commands[-1], "Runtime.disable")
        self.assertFalse(contexts.state(connection)["contexts"])
        connection.block = False
        self.assertEqual(await contexts.main_world(connection, "child"), "world-A")

    async def test_failed_temporary_disable_keeps_worlds_until_confirmed(self):
        contexts, connection = FrameContexts(), Connection()
        native_send = connection.send

        async def send(command):
            if command.gi_code.co_name == "disable":
                raise TimeoutError("disable did not reach Chrome")
            return await native_send(command)

        with patch.object(connection, "send", send):
            self.assertEqual(await contexts.main_world(connection, "child"), "world-A")
            self.assertIn("child", contexts.state(connection)["contexts"])
        await contexts.restore_runtime(connection)
        self.assertFalse(contexts.state(connection)["contexts"])

    async def test_anyio_cancellation_closes_temporary_runtime_before_retry(self):
        contexts, connection = FrameContexts(), Connection()
        connection.block = True
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(contexts.main_world, connection, "child")
            await connection.entered.wait()
            tasks.cancel_scope.cancel()
        self.assertEqual(connection.commands[-1], "Runtime.disable")
        self.assertFalse(contexts.state(connection)["contexts"])
        connection.block = False
        self.assertEqual(await contexts.main_world(connection, "child"), "world-A")

    async def test_destroyed_events_match_unique_world_and_watchers_do_not_retain_pages(
        self,
    ):
        contexts, connection = FrameContexts(), Connection()
        external = lambda event: None
        connection.add_handler(cdp.runtime.ConsoleAPICalled, external)
        await contexts.main_world(connection, "child")
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
