import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from extended_adapter.api import (
    ReadOnlyClient, SnapshotError, _NoRedirect, _collect_resource, collect_snapshot,
)


def page(rows, cursor=None):
    return json.dumps({"status": "OK", "data": rows,
                       "pagination": {"count": len(rows), "cursor": cursor}})


class ApiTests(unittest.TestCase):
    def test_all_cursor_pages_even_short_pages_and_large_ids(self):
        client = Mock()
        large = 9223372036854775808
        client.get.side_effect = [page([{"id": large}], large), page([{"id": 2}], 2), page([])]
        with tempfile.TemporaryDirectory() as directory:
            result = _collect_resource(client, Path(directory), "trades", "/user/trades", True, {}, 10)
            self.assertTrue(result["retrieval_complete"])
            self.assertEqual(result["rows"], 2)
            self.assertEqual(client.get.call_args_list[1].args[1]["cursor"], large)
            self.assertEqual(os.stat(Path(directory) / "trades.page-0001.json").st_mode & 0o777, 0o600)

    def test_cursor_loop_is_incomplete(self):
        client = Mock()
        client.get.side_effect = [page([{"id": 1}], 7), page([{"id": 2}], 7)]
        with tempfile.TemporaryDirectory() as directory:
            result = _collect_resource(client, Path(directory), "trades", "/user/trades", True, {}, 10)
        self.assertFalse(result["retrieval_complete"])
        self.assertEqual(result["error"], "pagination_loop")

    def test_empty_page_with_continuation_cursor_is_incomplete(self):
        client = Mock()
        client.get.return_value = page([], 123)
        with tempfile.TemporaryDirectory() as directory:
            result = _collect_resource(client, Path(directory), "trades", "/user/trades", True, {}, 10)
        self.assertFalse(result["retrieval_complete"])
        self.assertEqual(result["error"], "empty_page_with_cursor")

    def test_overlap_is_not_silently_deduplicated(self):
        client = Mock()
        client.get.side_effect = [page([{"id": 1}], 1), page([{"id": 1}], None)]
        with tempfile.TemporaryDirectory() as directory:
            result = _collect_resource(client, Path(directory), "trades", "/user/trades", True, {}, 10)
        self.assertEqual(result["error"], "overlapping_page_ids")

    def test_response_errors_do_not_leak_messages_or_keys(self):
        client = Mock()
        client.get.return_value = '{"status":"ERROR","error":{"message":"my-secret-key"}}'
        with tempfile.TemporaryDirectory() as directory:
            result = _collect_resource(client, Path(directory), "trades", "/user/trades", True, {}, 10)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertNotIn("my-secret-key", json.dumps(result))

    def test_exception_message_redacted(self):
        client = Mock()
        client.get.side_effect = RuntimeError("API key: secret-value")
        with tempfile.TemporaryDirectory() as directory:
            result = _collect_resource(client, Path(directory), "trades", "/user/trades", True, {}, 10)
        self.assertNotIn("secret-value", json.dumps(result))

    def test_missing_pagination_and_page_limit_are_incomplete(self):
        client = Mock()
        client.get.return_value = '{"status":"OK","data":[{"id":1}]}'
        with tempfile.TemporaryDirectory() as directory:
            result = _collect_resource(client, Path(directory), "trades", "/user/trades", True, {}, 1)
        self.assertEqual(result["error"], "missing_pagination")
        client.get.return_value = page([{"id": 1}], 1)
        with tempfile.TemporaryDirectory() as directory:
            result = _collect_resource(client, Path(directory), "trades", "/user/trades", True, {}, 1)
        self.assertEqual(result["error"], "page_limit_reached")

    def test_redirects_and_unknown_endpoints_are_refused(self):
        with self.assertRaisesRegex(SnapshotError, "redirect_refused"):
            _NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")
        with self.assertRaisesRegex(SnapshotError, "endpoint_not_allowed"):
            ReadOnlyClient("example-key").get("/user/order", {})

    def test_request_is_get_fixed_host_and_echoed_key_is_refused(self):
        client = ReadOnlyClient("example-key")
        response = Mock()
        response.read.return_value = b'{"status":"OK","data":"example-key"}'
        client._opener = Mock()
        client._opener.open.return_value.__enter__ = Mock(return_value=response)
        client._opener.open.return_value.__exit__ = Mock(return_value=False)
        with self.assertRaisesRegex(SnapshotError, "credential_echo_refused"):
            client.get("/user/trades", {"cursor": 1})
        request = client._opener.open.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.full_url, "https://api.starknet.extended.exchange/api/v1/user/trades?cursor=1")

    def test_manifest_keeps_partial_failure_and_interest_account(self):
        client = Mock()
        def get(path, params):
            if path == "/user/account/info":
                return '{"status":"OK","data":{"accountId":123}}'
            if path == "/user/balance":
                raise SnapshotError("http_404")
            if path == "/user/asset-operations":
                self.assertEqual(params, {"accountId": 123})
                return page([])
            if path in ("/user/trades", "/user/funding/history", "/user/positions/history", "/user/assetOperations"):
                return page([])
            if path == "/interest/payments":
                self.assertEqual(params, {"accountId": 123, "interval": "ALL"})
                return '{"status":"OK","data":{"items":[]}}'
            return '{"status":"OK","data":[]}'
        client.get.side_effect = get
        with tempfile.TemporaryDirectory() as directory:
            result = collect_snapshot(Path(directory) / "snapshot", "not-used", client=client)
            self.assertFalse(result["retrieval_complete"])
            self.assertFalse(result["ledger_complete"])
            self.assertEqual(result["sources"]["balance"]["error"], "http_404")
            self.assertTrue(result["sources"]["trades"]["retrieval_complete"])
            self.assertTrue(result["sources"]["interest"]["retrieval_complete"])
            with self.assertRaises(FileExistsError):
                collect_snapshot(Path(directory) / "snapshot", "not-used", client=client)


if __name__ == "__main__":
    unittest.main()
