"""Persistent paths for complete browser artifacts."""

import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4


def artifact_path(kind: str, suffix: str) -> Path:
    root = Path(
        os.environ.get("NODRIVER_ARTIFACTS_DIR")
        or Path.home() / ".nodriver-mcp" / "artifacts"
    ).expanduser()
    directory = root.resolve() / kind
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (
        datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:12] + suffix
    )
