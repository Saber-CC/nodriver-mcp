"""Native trace recording defaults and persistent raw trace exports."""

import asyncio
import gzip
import json
from pathlib import Path

from .artifacts import artifact_path


# Matches upstream DevTools defaults plus JS sampling and screenshots.
TRACE_CATEGORIES = [
    "-*",
    "blink.console",
    "blink.user_timing",
    "loading",
    "devtools.timeline",
    "disabled-by-default-devtools.target-rundown",
    "disabled-by-default-devtools.timeline.frame",
    "disabled-by-default-devtools.timeline.stack",
    "disabled-by-default-devtools.timeline",
    "disabled-by-default-devtools.v8-source-rundown-sources",
    "disabled-by-default-devtools.v8-source-rundown",
    "disabled-by-default-layout_shift.debug",
    "disabled-by-default-v8.inspector",
    "disabled-by-default-v8.cpu_profiler.hires",
    "disabled-by-default-lighthouse",
    "v8.execute",
    "v8",
    "cppgc",
    "navigation",
    "rail",
    "disabled-by-default-v8.cpu_profiler",
    "disabled-by-default-devtools.screenshot",
]


async def save_trace(data, file_path, duration_ms, config, data_loss=False):
    path = (
        Path(file_path).expanduser().resolve()
        if file_path
        else artifact_path("traces", ".json")
    )

    def save():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(data) if path.suffix == ".gz" else data)
        try:
            trace = json.loads(data)
            events = trace if isinstance(trace, list) else trace["traceEvents"]
            count = len(events)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(
                f"Invalid Chrome trace; raw data preserved at {path}: {exc}"
            ) from exc
        return {
            "file_path": str(path),
            "duration_ms": round(duration_ms, 2),
            "event_count": count,
            "raw_bytes": len(data),
            "data_loss_occurred": data_loss,
            "config": config,
        }

    try:
        return await asyncio.to_thread(save)
    except OSError as exc:
        raise ValueError(f"Cannot save trace to {path}: {exc}") from exc
