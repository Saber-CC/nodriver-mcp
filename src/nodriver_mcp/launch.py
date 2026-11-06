"""Browser startup and settings shared by lazy startup and explicit MCP startup."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import nodriver as uc
from nodriver.core._contradict import ContraDict
from websockets.exceptions import WebSocketException


logger = logging.getLogger("nodriver-mcp")
_DEVTOOLS_STARTUP_TIMEOUT = 10
_DEVTOOLS_PROBE_TIMEOUT = 0.5
_DEVTOOLS_POLL_INTERVAL = 0.1


class BrowserStartupError(RuntimeError):
    """A Chrome startup failure safe to report to the MCP client."""


def _read_devtools_version(host: str, port: int) -> dict:
    with closing(HTTPConnection(host, port, timeout=_DEVTOOLS_PROBE_TIMEOUT)) as conn:
        conn.request("GET", "/json/version")
        with conn.getresponse() as response:
            if response.status != 200:
                raise ConnectionError(f"Chrome DevTools returned HTTP {response.status}")
            info = json.load(response)
    url = info.get("webSocketDebuggerUrl") if isinstance(info, dict) else None
    if not isinstance(url, str) or not url:
        raise ValueError("Chrome DevTools did not provide a WebSocket URL.")
    return info


async def _connect_devtools(browser: uc.Browser) -> None:
    config = browser.config
    last_probe_error = None
    try:
        async with asyncio.timeout(_DEVTOOLS_STARTUP_TIMEOUT):
            while True:
                try:
                    info = await asyncio.to_thread(
                        _read_devtools_version, config.host, config.port
                    )
                except (OSError, HTTPException, ValueError) as exc:
                    last_probe_error = exc
                    await asyncio.sleep(_DEVTOOLS_POLL_INTERVAL)
                else:
                    break
            browser.info = ContraDict(info, silent=True)
            browser.websocket_url = info["webSocketDebuggerUrl"]
            last_probe_error = None
            await browser.attach()
    except TimeoutError as exc:
        raise BrowserStartupError(
            "Timed out waiting for the Chrome DevTools port "
            f"({_DEVTOOLS_STARTUP_TIMEOUT:g}s)."
        ) from (last_probe_error or exc)
    except (OSError, WebSocketException, uc.ProtocolException) as exc:
        raise BrowserStartupError("Could not connect to Chrome DevTools.") from exc


async def launch_chrome(
    browser: uc.Browser, initialize_page: Callable[[uc.Browser], Awaitable[uc.Tab]]
) -> uc.Tab:
    """Launch once and wait for the debugger and first page on that process."""
    config = browser.config
    executable = config.browser_executable_path
    if not executable or not Path(executable).is_file():
        raise FileNotFoundError(
            "Chrome executable not found. Set NODRIVER_BROWSER_PATH to its location."
        )
    config.host = "127.0.0.1"
    config.port = uc.util.free_port()
    if config._extensions:
        config.add_argument("--load-extension=" + ",".join(map(str, config._extensions)))

    logger.info("Starting Chrome (DevTools port=%s)", config.port)
    # Keep the process on the native Browser so the existing shutdown owns it.
    # Chrome's stderr is inherited for diagnostics, separate from MCP stdout.
    with anyio.CancelScope(shield=True):
        process = await asyncio.create_subprocess_exec(
            executable,
            *config(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            close_fds=os.name == "posix",
        )
        browser._process = process
        browser._process_pid = process.pid
        uc.util.get_registered_instances().add(browser)

    async def initialize() -> uc.Tab:
        await _connect_devtools(browser)
        return await initialize_page(browser)

    ready = asyncio.create_task(initialize())
    exited = asyncio.create_task(process.wait())
    try:
        await asyncio.wait((ready, exited), return_when=asyncio.FIRST_COMPLETED)
        exit_code = exited.result() if exited.done() else process.returncode
        if exit_code is not None:
            raise BrowserStartupError(
                f"Chrome exited during startup (exit code {exit_code})."
            )
        return ready.result()
    finally:
        ready.cancel()
        exited.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(ready, exited, return_exceptions=True)


def _string_list(variable: str) -> tuple[str, ...]:
    try:
        value = json.loads(os.environ.get(variable, "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{variable} must be a JSON array of strings.") from exc
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{variable} must be a JSON array of strings.")
    return tuple(value)


def _proxy_address(proxy: str | None) -> str | None:
    if proxy is None:
        proxy = os.environ.get("NODRIVER_PROXY") or None
    if proxy is None:
        return None  # Chrome's system proxy settings.
    proxy = proxy.strip()
    if not proxy:
        return ""  # An explicit request for a direct connection.
    address = proxy if "://" in proxy else "http://" + proxy
    try:
        parsed = urlsplit(address)
        if (
            parsed.scheme not in {"http", "https", "socks4", "socks5"}
            or not parsed.hostname
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(c.isspace() for c in address)
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "Supply a proxy such as http://127.0.0.1:7890 or socks5://127.0.0.1:1080."
        ) from None
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "Authenticated proxies require create_browser_context, which uses "
            "nodriver's native authentication forwarder."
        )
    return address.rstrip("/")


@dataclasses.dataclass(frozen=True)
class LaunchOptions:
    persistent: bool
    profile: str
    user_data_dir: str | None
    proxy: str | None
    headless: bool
    browser_path: str | None
    browser_args: tuple[str, ...]
    extensions: tuple[str, ...]

    @classmethod
    def resolve(
        cls,
        *,
        persistent: bool = False,
        profile: str = "default",
        proxy: str | None = None,
    ) -> LaunchOptions:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", profile):
            raise ValueError(
                "profile must be 1-64 letters, digits, underscores or hyphens."
            )
        if not persistent and profile != "default":
            raise ValueError("A named profile requires persistent=true.")
        root = Path.home() / ".nodriver-mcp"
        directory = None
        if persistent:
            if profile == "default":
                directory = Path(
                    os.environ.get("NODRIVER_USER_DATA_DIR") or root / "chrome-profile"
                )
            else:
                directory = root / "profiles" / profile
            directory = str(directory.expanduser().resolve())
        args = _string_list("NODRIVER_BROWSER_ARGS")
        if any(
            arg.split("=", 1)[0] in {"--user-data-dir", "--profile-directory"}
            for arg in args
        ):
            raise ValueError(
                "Select profiles with start_browser(persistent=true, profile=...) "
                "or NODRIVER_USER_DATA_DIR, not NODRIVER_BROWSER_ARGS."
            )
        proxy = _proxy_address(proxy)
        if proxy is not None:
            # An explicit launch choice takes precedence over inherited proxy flags.
            proxy_flags = {
                "--proxy-server",
                "--no-proxy-server",
                "--proxy-auto-detect",
                "--proxy-pac-url",
            }
            if any(arg in {"--proxy-server", "--proxy-pac-url"} for arg in args):
                raise ValueError(
                    "Use --proxy-server=address syntax in NODRIVER_BROWSER_ARGS."
                )
            args = tuple(arg for arg in args if arg.split("=", 1)[0] not in proxy_flags)
            args += (f"--proxy-server={proxy}" if proxy else "--no-proxy-server",)
        return cls(
            persistent=persistent,
            profile=profile,
            user_data_dir=directory,
            proxy=proxy,
            headless=os.environ.get("NODRIVER_HEADLESS", "").lower()
            in {"1", "true", "yes"},
            browser_path=os.environ.get("NODRIVER_BROWSER_PATH") or None,
            browser_args=args,
            extensions=_string_list("NODRIVER_EXTENSIONS"),
        )

    def config(self) -> uc.Config:
        if self.user_data_dir:
            Path(self.user_data_dir).mkdir(parents=True, exist_ok=True)
        return uc.Config(
            user_data_dir=self.user_data_dir,
            headless=self.headless,
            browser_executable_path=self.browser_path,
            browser_args=list(self.browser_args),
        )


class ProfileLease:
    """Prevent two MCP processes from opening the same persistent profile."""

    def __init__(self, directory: str):
        self.path = Path(directory) / ".nodriver-mcp.lock"
        self.file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise ValueError(
                f"Persistent profile is already in use: {self.path.parent}. "
                "Close the other browser or choose a different profile."
            ) from exc
        self.file = handle

    def release(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
