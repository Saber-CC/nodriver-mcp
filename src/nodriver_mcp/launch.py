"""Browser launch settings shared by lazy startup and explicit MCP startup."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import nodriver as uc


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
