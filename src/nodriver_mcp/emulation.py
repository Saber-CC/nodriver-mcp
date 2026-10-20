"""Page-scoped media/header overrides, applied before new OOP frames execute."""

import asyncio
import json
import logging
import re
import weakref

from nodriver import Tab, cdp

logger = logging.getLogger(__name__)

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

    async def sync_locked(self, tab, page_id, *, collect_network=True):
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
            if (
                collect_network
                and not isinstance(tab, FrameSession)
                and self.enable_network is not None
            ):
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


def attach_frames(enabled=True):
    return cdp.target.set_auto_attach(
        enabled,
        wait_for_debugger_on_start=enabled,
        flatten=True,
        filter_=cdp.target.TargetFilter([{"type": "iframe"}, {"exclude": True}]),
    )


class FrameSession:
    """Use nodriver's native session routing on a dedicated guard connection."""

    def __init__(self, guard, event):
        self.guard = guard
        self.session_id = event.session_id
        self.active = True

    async def send(self, command):
        if not self.active:
            command.close()
            raise ConnectionError("Frame override session was detached.")
        return await self.guard.send(command, sessionId=str(self.session_id))


class FrameOverrides:
    """Configure OOP frames before their first scripts and subresources run."""

    def __init__(self, overrides):
        self.overrides = overrides
        self.pages = {}

    async def watch(self, page):
        page_id = str(page.target_id)
        if page_id in self.pages:
            state = self.pages[page_id]
            await state["ready"].wait()
            if state.get("error") is not None:
                raise state["error"]
            return
        # Child Network events must not be mistaken for the page's events by
        # nodriver's target-wide handlers. This native connection only guards
        # startup; normal frame connections retain their own collectors.
        guard = Tab(target=page.target, parent=page)
        state = {
            "guard": guard,
            "sessions": {},
            "tasks": set(),
            "closing": False,
            "ready": asyncio.Event(),
        }
        self.pages[page_id] = state

        def attached(event):
            if event.target_info.type_ != "iframe" or state["closing"]:
                return
            session = FrameSession(guard, event)
            state["sessions"][str(event.session_id)] = session
            task = asyncio.create_task(self.initialize(session, page_id))
            state["tasks"].add(task)
            task.add_done_callback(state["tasks"].discard)

        def detached(event):
            session = state["sessions"].pop(str(event.session_id), None)
            if session is not None:
                session.active = False
                self.overrides.retire(session)

        guard.add_handler(cdp.target.AttachedToTarget, attached)
        guard.add_handler(cdp.target.DetachedFromTarget, detached)
        try:
            await asyncio.wait_for(guard.send(attach_frames()), 5)
        except BaseException as exc:
            state["error"] = exc
            await self.close(page_id)
            raise
        finally:
            state["ready"].set()

    async def initialize(self, session, page_id):
        try:
            async with asyncio.timeout(10):
                async with self.overrides.locks.setdefault(page_id, asyncio.Lock()):
                    await self.overrides.sync_locked(
                        session, page_id, collect_network=False
                    )
                await session.send(attach_frames())
        except Exception:
            logger.debug("Frame override initialization failed", exc_info=True)
        finally:
            try:
                await asyncio.wait_for(
                    session.send(cdp.runtime.run_if_waiting_for_debugger()), 3
                )
            except Exception:
                # Detaching also releases a target paused for this session.
                try:
                    await asyncio.wait_for(
                        session.guard.send(
                            cdp.target.detach_from_target(session_id=session.session_id)
                        ),
                        1,
                    )
                except Exception:
                    logger.debug("Frame override resume failed", exc_info=True)

    def connections(self, page_id):
        state = self.pages.get(page_id)
        return list(state["sessions"].values()) if state else []

    async def close(self, page_id):
        state = self.pages.pop(page_id, None)
        if state is None:
            return
        state["closing"] = True
        guard = state["guard"]
        try:
            await asyncio.wait_for(guard.send(attach_frames(False)), 3)
        except Exception:
            logger.debug("Frame override detach failed", exc_info=True)
        tasks = list(state["tasks"])
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for session in state["sessions"].values():
            session.active = False
            self.overrides.retire(session)
        try:
            await guard.aclose()
        except Exception:
            logger.debug("Frame override connection cleanup failed", exc_info=True)

    async def close_all(self):
        for page_id in list(self.pages):
            await self.close(page_id)
