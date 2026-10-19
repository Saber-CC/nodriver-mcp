import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from nodriver_mcp import performance as p

FIXTURE = Path(__file__).parent / "fixtures" / "web-dev-with-commit.json.gz"


class PerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_node_does_not_download_or_touch_the_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.json"
            trace.write_text('{"traceEvents":[]}')
            before = trace.read_bytes()
            with (
                patch.dict(os.environ, {"NODRIVER_NODE_PATH": ""}),
                patch.object(p.shutil, "which", return_value=None),
                patch.object(
                    p, "engine_package", side_effect=AssertionError("must not download")
                ),
            ):
                with self.assertRaisesRegex(ValueError, "Node.js"):
                    await p.run_engine(trace)
            self.assertEqual(trace.read_bytes(), before)

    async def test_bootstrap_rejects_bad_integrity_and_cleans_partial_cache(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "NODRIVER_DEVTOOLS_PACKAGE": "",
                    "NODRIVER_TRACE_ENGINE_CACHE_DIR": directory,
                },
            ),
            patch.object(p, "urlopen", return_value=io.BytesIO(b"wrong archive")),
        ):
            with self.assertRaisesRegex(ValueError, "SHA-512"):
                await asyncio.to_thread(p.engine_package)
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_analysis_preview_is_bounded_and_full_file_is_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.json"
            trace.write_text("original trace")
            target = Path(directory) / "nested" / "analysis.json"
            value = {
                "ok": True,
                "output": "analysis中文" * 1000,
                "web_vitals": [{"lcp_ms": 100, "inp_ms": None, "cls": 0}],
            }
            with patch.object(p, "run_engine", AsyncMock(return_value=value)) as run:
                report = await p.analyze_trace_file(
                    trace, file_path=str(target), max_output_chars=50
                )
                self.assertIn("preview truncated", report)
                self.assertLess(len(report), 600)
                self.assertEqual(
                    json.loads(target.read_text())["output"], value["output"]
                )
                self.assertEqual(trace.read_text(), "original trace")
                run.reset_mock()
                for options in (
                    {"file_path": str(trace)},
                    {"max_output_chars": 0},
                    {"insight_set_id": "one"},
                ):
                    with self.assertRaises(ValueError):
                        await p.analyze_trace_file(trace, **options)
                run.assert_not_awaited()

    async def test_timeout_and_cancellation_reap_the_analysis_process(self):
        class Process:
            def __init__(self):
                self.returncode = None
                self.started = asyncio.Event()
                self.reaped = False
                self.killed = False

            async def communicate(self):
                if self.returncode is not None:
                    self.reaped = True
                    return b"", b""
                self.started.set()
                await asyncio.Future()

            def kill(self):
                self.killed = True
                self.returncode = -9

        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.json"
            trace.write_text("{}")
            for cancel in (True, False):
                process = Process()
                with (
                    patch.object(p, "node_executable", return_value="node"),
                    patch.object(p, "engine_package", return_value=Path(directory)),
                    patch.object(
                        p.asyncio,
                        "create_subprocess_exec",
                        AsyncMock(return_value=process),
                    ),
                ):
                    task = asyncio.create_task(
                        p.run_engine(trace, timeout=30 if not cancel else 0)
                    )
                    await process.started.wait()
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        with self.assertRaisesRegex(TimeoutError, "raw trace remains"):
                            await task
                self.assertTrue(process.killed)
                self.assertTrue(process.reaped)
                self.assertEqual(trace.read_text(), "{}")


@unittest.skipUnless(
    os.environ.get("NODRIVER_MCP_PERFORMANCE_TESTS") == "1",
    "Native DevTools engine checks are opt-in",
)
class NativePerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_fixture_generates_real_insights_vitals_and_detailed_breakdown(
        self,
    ):
        result = await p.run_engine(FIXTURE)
        self.assertTrue(result["ok"])
        self.assertGreater(result["event_count"], 1000)
        self.assertEqual(len(result["insight_sets"]), 1)
        insight_set = result["insight_sets"][0]
        self.assertEqual(insight_set["errors"], {})
        self.assertGreaterEqual(len(insight_set["insights"]), 19)
        self.assertIn("RenderBlocking", {m["name"] for m in insight_set["insights"]})
        self.assertAlmostEqual(result["web_vitals"][0]["lcp_ms"], 129.211)
        self.assertIsNone(result["web_vitals"][0]["inp_ms"])
        self.assertEqual(result["web_vitals"][0]["cls"], 0)
        self.assertIn("LCP breakdown", result["output"])
        detailed = await p.run_engine(FIXTURE, insight_set["id"], "LCPBreakdown")
        self.assertIn("Time to first byte", detailed["output"])
        self.assertIn("129", detailed["output"])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "native-analysis.json"
            report = await p.analyze_trace_file(
                FIXTURE, file_path=str(target), max_output_chars=100
            )
            self.assertIn("preview truncated", report)
            self.assertEqual(
                json.loads(target.read_text())["insight_sets"][0]["errors"], {}
            )

    async def test_invalid_insight_and_empty_trace_return_engine_reasons(self):
        with self.assertRaisesRegex(ValueError, "Unknown insight_set_id"):
            await p.run_engine(FIXTURE, "invalid", "LCPBreakdown")
        with self.assertRaisesRegex(ValueError, "Unknown or unavailable insight_name"):
            await p.run_engine(FIXTURE, "NAVIGATION_0", "not-a-model")
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "empty.json"
            trace.write_text('{"traceEvents":[]}')
            with self.assertRaisesRegex(ValueError, "No trace events"):
                await p.run_engine(trace)
