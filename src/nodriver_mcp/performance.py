"""Offline analysis using the pinned, published upstream DevTools Trace Engine."""

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from .artifacts import artifact_path

logger = logging.getLogger(__name__)
ENGINE_VERSION = "1.11.0"
ENGINE_URL = (
    "https://registry.npmjs.org/chrome-devtools-mcp/-/chrome-devtools-mcp-1.11.0.tgz"
)
ENGINE_SHA512 = "CytJ/5tnzrS+J/74be2P5aCOsS/mmGRHys/Si+IYC4ucrbnWAmES/dYdxGJjeUCECRcoFUilZAysdjlwqUjXIA=="


def valid_package(root):
    try:
        info = json.loads((root / "package.json").read_text(encoding="utf-8"))
        return (
            isinstance(info, dict)
            and info.get("name") == "chrome-devtools-mcp"
            and info.get("version") == ENGINE_VERSION
            and (root / "build/src/third_party/index.js").is_file()
            and (root / "build/src/processors/ChunkedTraceParser.js").is_file()
        )
    except (OSError, ValueError):
        return False


def engine_package():
    override = os.environ.get("NODRIVER_DEVTOOLS_PACKAGE")
    if override:
        root = Path(override).expanduser().resolve()
        if not valid_package(root):
            raise ValueError(
                f"NODRIVER_DEVTOOLS_PACKAGE must point to a published chrome-devtools-mcp {ENGINE_VERSION} package: {root}"
            )
        return root
    cache = (
        Path(
            os.environ.get("NODRIVER_TRACE_ENGINE_CACHE_DIR")
            or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
            / "nodriver-mcp"
            / "trace-engine"
        )
        .expanduser()
        .resolve()
    )
    root = cache / ENGINE_VERSION
    if valid_package(root):
        return root
    try:
        cache.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".download-", dir=cache))
    except OSError as exc:
        raise ValueError(f"Cannot create trace-engine cache at {cache}: {exc}") from exc
    try:
        archive = stage / "engine.tgz"
        digest = hashlib.sha512()
        logger.info("Fetching pinned DevTools trace engine %s", ENGINE_VERSION)
        with (
            urlopen(
                Request(ENGINE_URL, headers={"User-Agent": "nodriver-mcp"}), timeout=30
            ) as response,
            archive.open("wb") as output,
        ):
            length = 0
            while block := response.read(65536):
                length += len(block)
                if length > 50 * 1024 * 1024:
                    raise ValueError("Trace-engine archive exceeds its size limit.")
                digest.update(block)
                output.write(block)
        if base64.b64encode(digest.digest()).decode() != ENGINE_SHA512:
            raise ValueError("Pinned trace-engine archive failed SHA-512 verification.")
        with tarfile.open(archive) as source:
            members = [
                m
                for m in source.getmembers()
                if (m.isfile() or m.isdir())
                and (
                    m.name
                    in {
                        "package/package.json",
                        "package/LICENSE",
                        "package/build/src/processors/ChunkedTraceParser.js",
                    }
                    or m.name.startswith("package/build/src/third_party/")
                )
            ]
            source.extractall(stage, members=members, filter="data")
        package = stage / "package"
        if not valid_package(package):
            raise ValueError("Downloaded trace-engine package is incomplete.")
        try:
            package.rename(root)
        except OSError:
            if not valid_package(root):
                raise ValueError(
                    f"Cannot install trace-engine cache at {root}; remove an invalid cache entry and retry."
                ) from None
        return root
    except (OSError, URLError, tarfile.TarError) as exc:
        raise ValueError(f"Cannot prepare DevTools trace engine: {exc}") from exc
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def node_executable():
    node = os.environ.get("NODRIVER_NODE_PATH") or shutil.which("node")
    if not node:
        raise ValueError(
            "Trace analysis requires Node.js 22.12+ (or NODRIVER_NODE_PATH). Browser tools and raw trace exports work without Node.js."
        )
    return node


def bridge_path():
    return Path(__file__).with_name("trace_engine.mjs")


async def run_engine(trace, insight_set_id="", insight_name="", timeout=60000):
    if timeout < 0:
        raise ValueError("Analysis timeout must be nonnegative.")
    trace = Path(trace).expanduser().resolve()
    if not trace.is_file():
        raise ValueError(f"Trace file does not exist: {trace}")
    node = node_executable()

    async def run():
        root = await asyncio.to_thread(engine_package)
        try:
            process = await asyncio.create_subprocess_exec(
                node,
                str(bridge_path()),
                str(root / "build/src/third_party/index.js"),
                str(trace),
                insight_set_id,
                insight_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ValueError(f"Cannot start Node.js trace analysis: {exc}") from exc
        try:
            output, error = await process.communicate()
        except BaseException:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.shield(process.communicate())
            raise
        try:
            result = json.loads(output)
        except ValueError as exc:
            raise ValueError(
                f"DevTools engine returned invalid output (Node.js 22.12+ required): {error.decode(errors='replace')[:1000]}"
            ) from exc
        if not isinstance(result, dict):
            raise ValueError("DevTools engine returned an invalid result object.")
        if process.returncode or not result.get("ok"):
            raise ValueError(
                "DevTools trace analysis failed: "
                + str(result.get("error") or error.decode(errors="replace"))[:2000]
            )
        return result

    try:
        return await asyncio.wait_for(run(), timeout / 1000) if timeout else await run()
    except TimeoutError as exc:
        raise TimeoutError(
            f"Trace analysis exceeded {timeout}ms; raw trace remains at {trace}."
        ) from exc


async def analyze_trace_file(
    trace_path,
    *,
    insight_set_id="",
    insight_name="",
    file_path="",
    max_output_chars=20000,
    timeout=60000,
):
    if max_output_chars < 1:
        raise ValueError("max_output_chars must be positive.")
    if bool(insight_name) != bool(insight_set_id):
        raise ValueError(
            "Provide both insight_set_id and insight_name for detailed analysis."
        )
    if (
        file_path
        and Path(file_path).expanduser().resolve()
        == Path(trace_path).expanduser().resolve()
    ):
        raise ValueError("Analysis file_path must differ from the original trace_path.")
    started = time.monotonic()
    result = await run_engine(trace_path, insight_set_id, insight_name, timeout)
    result.update(
        trace_path=str(Path(trace_path).expanduser().resolve()),
        engine=f"chrome-devtools-mcp@{ENGINE_VERSION}",
        analysis_duration_ms=round((time.monotonic() - started) * 1000, 2),
    )
    target = Path(file_path).expanduser().resolve() if file_path else None
    try:
        target = target or artifact_path("performance", ".json")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            target.write_text,
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        raise ValueError(f"Cannot save analysis to {target}: {exc}") from exc
    body = (
        "Observed Web Vitals (lab):\n"
        + json.dumps(result.get("web_vitals", []), ensure_ascii=False, indent=2)
        + "\n\n"
        + result["output"]
    )
    return (
        f"Engine: {result['engine']}\nTrace file: {json.dumps(result['trace_path'], ensure_ascii=False)}\n"
        f"Analysis file: {json.dumps(str(target), ensure_ascii=False)}\n\n"
        + body[:max_output_chars]
        + (
            f"\n<preview truncated: {len(body)} characters; complete analysis is in the file>"
            if len(body) > max_output_chars
            else ""
        )
    )
