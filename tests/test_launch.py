import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import nodriver as uc

from nodriver_mcp import server as s
from nodriver_mcp.launch import LaunchOptions, ProfileLease


ENV = {
    "NODRIVER_PROXY": "",
    "NODRIVER_USER_DATA_DIR": "",
    "NODRIVER_BROWSER_PATH": "",
    "NODRIVER_BROWSER_ARGS": "[]",
    "NODRIVER_EXTENSIONS": "[]",
    "NODRIVER_HEADLESS": "true",
}


class LaunchOptionsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_default_is_temporary_even_with_persistent_directory_configured(self):
        with patch.dict(os.environ, {"NODRIVER_USER_DATA_DIR": "/unused/persistent"}):
            options = LaunchOptions.resolve()
        self.assertFalse(options.persistent)
        self.assertIsNone(options.user_data_dir)

    def test_default_persistent_profile_reuses_the_legacy_directory(self):
        self.assertEqual(
            LaunchOptions.resolve(persistent=True).user_data_dir,
            str(Path.home() / ".nodriver-mcp" / "chrome-profile"),
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"NODRIVER_USER_DATA_DIR": directory}):
                self.assertEqual(
                    LaunchOptions.resolve(persistent=True).user_data_dir,
                    str(Path(directory).resolve()),
                )

    def test_named_profiles_are_distinct_and_cannot_escape_their_directory(self):
        a = LaunchOptions.resolve(persistent=True, profile="work")
        b = LaunchOptions.resolve(persistent=True, profile="personal")
        self.assertNotEqual(a.user_data_dir, b.user_data_dir)
        self.assertEqual(Path(a.user_data_dir).parent.name, "profiles")
        for profile in ("../other", "/tmp/other", ".", "", "x" * 65):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                LaunchOptions.resolve(persistent=True, profile=profile)
        with self.assertRaisesRegex(ValueError, "persistent=true"):
            LaunchOptions.resolve(profile="work")

    def test_explicit_proxy_and_direct_access_override_inherited_settings(self):
        with patch.dict(
            os.environ,
            {
                "NODRIVER_PROXY": "http://127.0.0.1:8000",
                "NODRIVER_BROWSER_ARGS": '["--proxy-server=http://old:80", "--no-proxy-server", "--disable-gpu"]',
            },
        ):
            options = LaunchOptions.resolve(proxy="http://127.0.0.1:7890")
            self.assertEqual(options.proxy, "http://127.0.0.1:7890")
            self.assertEqual(
                options.browser_args,
                ("--disable-gpu", "--proxy-server=http://127.0.0.1:7890"),
            )
            direct = LaunchOptions.resolve(proxy="")
            self.assertEqual(
                direct.browser_args, ("--disable-gpu", "--no-proxy-server")
            )
            self.assertEqual(LaunchOptions.resolve().proxy, "http://127.0.0.1:8000")

    def test_proxy_validation_accepts_native_schemes_and_rejects_invalid_addresses(
        self,
    ):
        for value, expected in (
            ("127.0.0.1:7890", "http://127.0.0.1:7890"),
            ("socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
            ("http://[::1]:7890/", "http://[::1]:7890"),
        ):
            with self.subTest(value=value):
                self.assertEqual(LaunchOptions.resolve(proxy=value).proxy, expected)
        for value in (
            "http://",
            "http://host:99999",
            "ftp://host:21",
            "http://host:80/path",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                LaunchOptions.resolve(proxy=value)
        with self.assertRaises(ValueError) as failure:
            LaunchOptions.resolve(proxy="http://user:secret@host:80")
        self.assertNotIn("secret", str(failure.exception))

    def test_profile_flags_and_malformed_configuration_are_rejected_before_launch(self):
        for variable, value in (
            ("NODRIVER_BROWSER_ARGS", "not-json"),
            ("NODRIVER_EXTENSIONS", "{}"),
            ("NODRIVER_BROWSER_ARGS", '["--user-data-dir=/another/profile"]'),
            ("NODRIVER_BROWSER_ARGS", '["--profile-directory", "Default"]'),
        ):
            with self.subTest(variable=variable, value=value):
                with (
                    patch.dict(os.environ, {variable: value}),
                    self.assertRaises(ValueError),
                ):
                    LaunchOptions.resolve()

    def test_profile_lease_prevents_concurrent_use_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = ProfileLease(directory), ProfileLease(directory)
            try:
                first.acquire()
                with self.assertRaisesRegex(ValueError, "already in use"):
                    second.acquire()
                first.release()
                second.acquire()
            finally:
                first.release()
                second.release()


class LaunchFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        s._browser_lock = asyncio.Lock()
        self.env = patch.dict(
            os.environ,
            {**ENV, "NODRIVER_BROWSER_PATH": "/nonexistent/nodriver-test-browser"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    async def asyncTearDown(self):
        await s._shutdown_browser()

    async def test_failed_start_removes_native_temporary_profile_and_can_retry(self):
        configs = []
        config_class = uc.Config

        def capture_config(**kwargs):
            config = config_class(**kwargs)
            configs.append(config)
            return config

        with patch.object(s.uc, "Config", side_effect=capture_config):
            for _ in range(2):
                with self.assertRaises(FileNotFoundError):
                    await s.start_browser()
                self.assertIsNone(s._browser)
                self.assertIsNone(s._launch_options)
                self.assertIsNone(s._profile_lease)
                self.assertFalse(Path(configs[-1].user_data_dir).exists())
        self.assertNotEqual(configs[0].user_data_dir, configs[1].user_data_dir)

    async def test_failed_persistent_start_releases_lease_without_deleting_profile(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with patch.dict(os.environ, {"NODRIVER_USER_DATA_DIR": directory}):
                with self.assertRaises(FileNotFoundError):
                    await s.start_browser(persistent=True)
                self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
                lease = ProfileLease(directory)
                try:
                    lease.acquire()
                finally:
                    lease.release()
