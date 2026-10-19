import base64
import itertools
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import nodriver as uc

from nodriver_mcp.network import NetworkCollector, body_value, request_details


def response(status=200):
    return NS(
        status=status,
        headers={"Content-Type": "text/plain"},
        mime_type="text/plain",
        url="https://test/final",
    )


def request(redirect=None, extra=False, url="https://test/start"):
    return NS(
        request_id="wire",
        request=NS(
            url=url,
            method="GET",
            headers={"x-base": "1"},
            post_data=None,
            has_post_data=False,
        ),
        timestamp=1,
        type_=None,
        redirect_response=redirect,
        redirect_has_extra_info=extra,
    )


class Extra(NS):
    def to_json(self):
        return {
            "requestId": "wire",
            "headers": self.headers,
            **(
                {"statusCode": self.status_code} if hasattr(self, "status_code") else {}
            ),
        }


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tab = NS(send=AsyncMock(), handlers=defaultdict(list))
        self.records = []
        ids = itertools.count(10)

        def collect(record):
            record.update(reqid=next(ids), connection=self.tab)
            self.records.append(record)

        self.collector = NetworkCollector(self.tab, collect)

    async def test_early_and_late_extra_info_stays_with_its_redirect_hop(self):
        c = self.collector
        c.request_extra(Extra(request_id="wire", headers={"Cookie": "first"}))
        c.response_extra(
            Extra(request_id="wire", headers={"Set-Cookie": "a=1"}, status_code=302)
        )
        c.request(request())
        c.request(request(response(302), True, "https://test/cache"))
        # A cached redirect has no extra events and must not consume the final ones.
        c.request(request(response(301), False, "https://test/final"))
        c.response(NS(request_id="wire", response=response(), has_extra_info=True))
        c.response_extra(
            Extra(
                request_id="wire",
                headers={"content-type": "application/json"},
                status_code=304,
            )
        )
        c.request_extra(Extra(request_id="wire", headers={"Cookie": "last"}))
        first, middle, last = self.records
        self.assertEqual([r["reqid"] for r in self.records], [10, 11, 12])
        self.assertEqual(first["headers"]["Cookie"], "first")
        self.assertEqual(first["response_headers"]["Set-Cookie"], "a=1")
        self.assertNotIn("Cookie", middle["headers"])
        self.assertEqual(last["headers"]["Cookie"], "last")
        self.assertEqual(last["response_headers"], {"content-type": "application/json"})
        self.assertEqual(last["status"], 200)
        self.assertEqual(last["wire_status"], 304)
        self.assertEqual(last["redirect_from"], 11)
        self.assertEqual(first["redirect_to"], 11)
        self.assertEqual([r["reqid"] for r in last["redirect_chain"]], [10, 11])
        self.assertEqual((await body_value(first, True))[0], "redirect")
        self.tab.send.assert_not_awaited()

    async def test_body_states_do_not_query_pending_failed_or_retired_connections(self):
        c = self.collector
        c.request(request())
        record = self.records[0]
        self.assertEqual((await body_value(record, True))[0], "pending")
        c.failed(NS(request_id="wire", error_text="net::ERR_ABORTED"))
        self.assertEqual((await body_value(record, True))[0], "failed")
        record.pop("error")
        self.tab.remove_handler = lambda event, callback: None
        c.remove()
        self.assertEqual((await body_value(record, True))[0], "connection_retired")
        self.tab.send.assert_not_awaited()

    async def test_evicted_unavailable_and_protocol_errors_preserve_reasons(self):
        record = {"connection": self.tab, "id": "wire", "finished": True}
        for reason, state in [
            ("Request content was evicted from inspector cache", "evicted"),
            ("No data found for resource with given identifier", "unavailable"),
            ("Unsupported command", "protocol_error"),
        ]:
            self.tab.send.side_effect = uc.ProtocolException(
                {"code": -32000, "message": reason}
            )
            value = await body_value(record, True)
            self.assertEqual(value[0], state)
            self.assertIn(reason, value[3])

    async def test_body_limits_cover_post_data_and_exports_keep_complete_bytes(self):
        payload = "long中文" * 1000
        binary = bytes(range(256)) * 10
        record = {
            "connection": self.tab,
            "id": "wire",
            "post_data": payload,
            "finished": True,
        }
        self.tab.send.return_value = (base64.b64encode(binary).decode(), True)
        detail = await request_details(record, max_body_chars=50)
        self.assertLess(len(detail), 800)
        self.assertIn("preview truncated", detail)
        self.assertNotIn(payload, detail)
        with tempfile.TemporaryDirectory() as directory:
            post, result = Path(directory) / "post.txt", Path(directory) / "body.bin"
            await request_details(record, str(post), str(result), max_body_chars=5)
            self.assertEqual(post.read_text(), payload)
            self.assertEqual(result.read_bytes(), binary)
        self.tab.send.reset_mock()
        detail = await request_details(record, include_body=False)
        self.assertIn('"response_body_state": "not_requested"', detail)
        self.tab.send.assert_not_awaited()

    async def test_empty_body_is_success_and_is_exported(self):
        self.tab.send.return_value = ("", False)
        record = {
            "connection": self.tab,
            "id": "wire",
            "finished": True,
            "has_post_data": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty"
            detail = await request_details(record, response_file_path=str(path))
            self.assertEqual(path.read_bytes(), b"")
            self.assertIn('"response_body_state": "empty"', detail)
