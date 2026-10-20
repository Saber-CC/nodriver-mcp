"""Explicit opt-in for detectable Runtime event collection."""

RUNTIME_ENABLE_WARNING = (
    "Runtime.enable can expose CDP/automation to websites and trigger bot detection "
    "or blocked requests; disabling it later may not undo detection. Do not enable "
    "it casually or as a troubleshooting fallback. Use only for debugging explicitly "
    "requested by a user who accepts this risk."
)


def require_runtime_enable(allowed: bool) -> None:
    if allowed is not True:
        raise ValueError(
            "Runtime.enable is blocked by default. "
            + RUNTIME_ENABLE_WARNING
            + " Pass allow_runtime_enable=true only for that requested debugging."
        )
