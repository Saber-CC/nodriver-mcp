"""Page-scoped media/header overrides, synchronized to discovered frame sessions."""

import asyncio
import json
import re
import weakref

from nodriver import cdp

MEDIA_VALUES = {
    "prefers-color-scheme": {"light", "dark"},
    "forced-colors": {"active", "none"},
    "prefers-contrast": {"more", "less", "custom", "no-preference"},
    "prefers-reduced-motion": {"reduce", "no-preference"},
}


def media_updates(**values):
    names = {
        "color_scheme": "prefers-color-scheme",
        "forced_colors": "forced-colors",
        "prefers_contrast": "prefers-contrast",
        "prefers_reduced_motion": "prefers-reduced-motion",
    }
    updates = {}
    for name, value in values.items():
        if not value:
            continue
        wire = names[name]
        if value != "auto" and value not in MEDIA_VALUES[wire]:
            raise ValueError(f"Invalid {name}: {value!r}.")
        updates[wire] = None if value == "auto" else value
    return updates


def http_headers(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value) if value else {}
        except ValueError as exc:
            raise ValueError(
                f"extra_http_headers must be a JSON object: {exc}"
            ) from exc
    if not isinstance(value, dict):
        raise ValueError("extra_http_headers must be a string-valued object.")
    result = {}
    for name, content in value.items():
        if not isinstance(name, str) or not re.fullmatch(
            r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name
        ):
            raise ValueError(f"Invalid HTTP header name: {name!r}.")
        if not isinstance(content, str) or any(
            c in content for c in ("\r", "\n", "\0")
        ):
            raise ValueError(
                f"HTTP header {name!r} must be a string without CR/LF/NUL."
            )
        result[name] = content
    return result


class EmulationOverrides:
    def __init__(self, enable_network=None):
        self.enable_network = enable_network
        self.states, self.locks, self.applied = {}, {}, {}

    async def sync_locked(self, tab, page_id):
        state = self.states.get(page_id)
        if state is None:
            return
        previous = self.applied.get(id(tab))
        if previous and previous[0]() is tab and previous[1] is state:
            return
        if "media" in state:
            await tab.send(
                cdp.emulation.set_emulated_media(
                    features=[
                        cdp.emulation.MediaFeature(name, value)
                        for name, value in state["media"].items()
                    ]
                )
            )
        if "headers" in state:
            if self.enable_network is not None:
                await self.enable_network(tab)
            else:
                await tab.send(cdp.network.enable())
            await tab.send(
                cdp.network.set_extra_http_headers(
                    cdp.network.Headers(state["headers"])
                )
            )
        self.applied[id(tab)] = (weakref.ref(tab), state)

    async def sync(self, tab, page_id):
        if page_id not in self.states:
            return
        async with self.locks.setdefault(page_id, asyncio.Lock()):
            await self.sync_locked(tab, page_id)

    async def update(
        self, page_id, connections, updates=None, headers=None, reset=False
    ):
        async with self.locks.setdefault(page_id, asyncio.Lock()):
            state = dict(self.states.get(page_id, {}))
            if reset:
                state = {"media": {}, "headers": {}}
            else:
                if updates:
                    media = dict(state.get("media", {}))
                    for key, value in updates.items():
                        if value is None:
                            media.pop(key, None)
                        else:
                            media[key] = value
                    state["media"] = media
                if headers is not None:
                    state["headers"] = dict(headers)
            self.states[page_id] = state
            for tab in {id(tab): tab for tab in connections}.values():
                await self.sync_locked(tab, page_id)

    def retire(self, tab):
        self.applied.pop(id(tab), None)

    def forget(self, page_id):
        state = self.states.pop(page_id, None)
        self.locks.pop(page_id, None)
        for key, (_, applied) in list(self.applied.items()):
            if applied is state:
                self.applied.pop(key, None)

    def clear(self):
        self.states.clear()
        self.locks.clear()
        self.applied.clear()
