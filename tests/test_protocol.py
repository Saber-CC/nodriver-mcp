import unittest

from nodriver_mcp.protocol import (
    build_command,
    describe_commands,
    json_value,
    resolve_event,
)


class ProtocolTests(unittest.TestCase):
    def test_typed_cookie_expiration_and_enum_are_serialized(self):
        command = build_command(
            "Network",
            "setCookie",
            {
                "name": "session",
                "value": "test",
                "url": "https://example.test",
                "expires": 1900000000.0,
                "sameSite": "Lax",
            },
        )
        payload = next(command)
        self.assertEqual(payload["params"]["expires"], 1900000000.0)
        self.assertEqual(payload["params"]["sameSite"], "Lax")
        self.assertNotIn("sameParty", payload["params"])

    def test_nested_official_dataclass_and_camel_case_parameters(self):
        command = build_command(
            "Emulation",
            "setDeviceMetricsOverride",
            {
                "width": 412,
                "height": 915,
                "deviceScaleFactor": 2.625,
                "mobile": True,
                "screenOrientation": {"type": "portraitPrimary", "angle": 0},
            },
        )
        self.assertEqual(
            next(command)["params"]["screenOrientation"],
            {"type": "portraitPrimary", "angle": 0},
        )

    def test_smart_card_enum_and_binary_array(self):
        command = build_command(
            "SmartCardEmulation",
            "reportDataResult",
            {"requestId": "probe", "data": "aGVsbG8="},
        )
        self.assertEqual(next(command)["method"], "SmartCardEmulation.reportDataResult")

    def test_rejects_unknown_duplicate_and_missing_parameters(self):
        for parameters in (
            {"typo": 1},
            {"width": 1},
            {
                "width": 1,
                "height": 1,
                "device_scale_factor": 1,
                "deviceScaleFactor": 2,
                "mobile": False,
            },
        ):
            with (
                self.subTest(parameters=parameters),
                self.assertRaises((ValueError, TypeError)),
            ):
                build_command("emulation", "set_device_metrics_override", parameters)

    def test_new_domains_are_discoverable_and_events_resolve(self):
        for domain in (
            "WebMCP",
            "SmartCardEmulation",
            "FindInPage",
            "DigitalCredentials",
            "Ads",
        ):
            self.assertTrue(describe_commands(domain))
        method, event = resolve_event("web_mcp.ToolsAdded")
        self.assertEqual(method, "WebMCP.toolsAdded")
        self.assertEqual(event.__name__, "ToolsAdded")

    def test_native_event_objects_become_json_values(self):
        import nodriver as uc

        event = uc.cdp.web_mcp.ToolsRemoved(
            [
                uc.cdp.web_mcp.RemovedTool(
                    name="probe", frame_id=uc.cdp.page.FrameId("frame")
                )
            ]
        )
        self.assertEqual(json_value(event)["tools"][0]["frameId"], "frame")

    def test_native_headers_returning_self_from_to_json_do_not_recurse(self):
        import nodriver as uc

        self.assertEqual(
            json_value(uc.cdp.network.Headers({"Content-Type": "text/html"})),
            {"Content-Type": "text/html"},
        )
