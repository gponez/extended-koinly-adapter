"""Read-only Extended snapshots; no orders, signatures or private keys.

Endpoint/pagination reference: https://api.docs.extended.exchange/ (2026-10-27).
Snapshots are evidence for later normalization, not automatically tax-ready data.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE_URL = "https://api.starknet.extended.exchange/api/v1"
SOURCES = (
    ("account", "/user/account/info", False),
    ("balance", "/user/balance", False),
    ("spot_balances", "/user/spot/balances", False),
    ("markets", "/info/markets", False),
    ("assets", "/info/assets", False),
    ("open_positions", "/user/positions", False),
    ("trades", "/user/trades", True),
    ("funding", "/user/funding/history", True),
    ("positions_history", "/user/positions/history", True),
    ("asset_operations", "/user/assetOperations", True),
    # Newer web-app history includes non-USDC collateral; transfer records are
    # sender-oriented. Preserve both sources and reconcile before conversion.
    ("asset_operations_v2", "/user/asset-operations", True),
    ("rebates_stats", "/user/rebates/stats", False),
    ("interest", "/interest/payments", False),
)
ALLOWED_PATHS = frozenset(path for _, path, _ in SOURCES)


class SnapshotError(Exception):
    """Only fixed, non-sensitive diagnostic codes should be passed here."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise forwards custom headers, including X-Api-Key.
        raise SnapshotError("redirect_refused")


class ReadOnlyClient:
    def __init__(self, api_key: str):
        if not isinstance(api_key, str) or not api_key.strip() or any(
            char in api_key for char in "\r\n"
        ):
            raise ValueError("A nonempty API key without line breaks is required")
        self._api_key = api_key
        self._opener = build_opener(_NoRedirect())

    def get(self, path: str, params: dict) -> str:
        if path not in ALLOWED_PATHS:
            raise SnapshotError("endpoint_not_allowed")
        query = urlencode(params)
        url = BASE_URL + path + ("?" + query if query else "")
        request = Request(url, headers={
            "X-Api-Key": self._api_key,
            "User-Agent": "extended-koinly-adapter/0.1 (read-only snapshot)",
            "Accept": "application/json",
        }, method="GET")
        for attempt in range(3):
            try:
                with self._opener.open(request, timeout=30) as response:
                    body = response.read(32 * 1024 * 1024 + 1)
                    if len(body) > 32 * 1024 * 1024:
                        raise SnapshotError("response_too_large")
                    text = body.decode("utf-8")
                    # Do not persist an echoed credential, even on HTTP 200.
                    if self._api_key in text:
                        raise SnapshotError("credential_echo_refused")
                    return text
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise SnapshotError(f"http_{code}") from None
            except (URLError, TimeoutError, OSError, UnicodeError):
                raise SnapshotError("transport_or_encoding_error") from None
        raise SnapshotError("retry_limit")


def _write_private(path: Path, text: str) -> str:
    raw = text.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()


def _decode(text: str) -> dict:
    try:
        # Keep integer IDs exact and decimal literals as strings in inspection.
        # The original JSON text is retained unchanged in page files.
        payload = json.loads(text, parse_float=str)
    except (ValueError, TypeError):
        raise SnapshotError("invalid_json") from None
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        raise SnapshotError("api_error_or_missing_status")
    if "data" not in payload:
        raise SnapshotError("missing_data")
    return payload


def _collect_resource(client, directory, name, path, paginated, params, max_pages):
    result = {"endpoint": path, "parameters": dict(params), "pages": [],
              "rows": 0, "retrieval_complete": False}
    seen_cursors, seen_ids = set(), set()
    try:
        for number in range(1, max_pages + 1):
            text = client.get(path, params)
            payload = _decode(text)
            data = payload["data"]
            if paginated and not isinstance(data, list):
                raise SnapshotError("expected_list_data")
            rows = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
            if not isinstance(rows, list):
                raise SnapshotError("expected_items_list")
            filename = f"{name}.page-{number:04d}.json"
            digest = _write_private(directory / filename, text)
            result["pages"].append({"file": filename, "sha256": digest, "rows": len(rows)})
            result["rows"] += len(rows)
            if not paginated:
                # Undocumented pagination must not be silently ignored.
                if payload.get("pagination", {}).get("cursor") is not None:
                    raise SnapshotError("unexpected_pagination")
                result["retrieval_complete"] = True
                break
            ids = [str(row["id"]) for row in rows if isinstance(row, dict) and "id" in row]
            if len(ids) != len(set(ids)) or seen_ids.intersection(ids):
                raise SnapshotError("overlapping_page_ids")
            seen_ids.update(ids)
            pagination = payload.get("pagination")
            if not isinstance(pagination, dict):
                raise SnapshotError("missing_pagination")
            if pagination.get("count") != len(rows):
                raise SnapshotError("pagination_count_mismatch")
            cursor = pagination.get("cursor")
            if not rows and cursor is not None:
                raise SnapshotError("empty_page_with_cursor")
            if cursor is None:
                result["retrieval_complete"] = True
                break
            if isinstance(cursor, bool) or not isinstance(cursor, (str, int)):
                raise SnapshotError("invalid_cursor")
            if str(cursor) in seen_cursors:
                raise SnapshotError("pagination_loop")
            seen_cursors.add(str(cursor))
            params = {**params, "cursor": cursor}
        else:
            raise SnapshotError("page_limit_reached")
    except SnapshotError as exc:
        result["error"] = str(exc)
    except Exception:
        # Never serialize request objects, exception messages or response bodies.
        result["error"] = "unexpected_collection_error"
    return result


def collect_snapshot(output_dir, api_key, start_time_ms=0, *, max_pages=10000,
                     client=None):
    """Save API evidence for one authenticated account and return its manifest.

    ``output_dir`` must be new. ``start_time_ms`` only filters funding; other
    histories are fetched in full. ``client`` is a test hook with get(path, params).
    Success means pagination finished, not that a tax ledger is complete. No
    authenticated network requests are made unless this function is called.
    """
    if type(start_time_ms) is not int or start_time_ms < 0:
        raise ValueError("start_time_ms must be a nonnegative integer")
    if type(max_pages) is not int or max_pages < 1:
        raise ValueError("max_pages must be a positive integer")
    client = client or ReadOnlyClient(api_key)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    manifest = {
        "base_url": BASE_URL, "started_at": datetime.now(timezone.utc).isoformat(),
        "sources": {}, "ledger_complete": False,
        "limitations": [
            "This is a sequential snapshot, not an atomic account snapshot.",
            "Rebates API provides cumulative statistics, not dated rebate payments.",
            "Interest uses interval=ALL; documentation does not describe pagination or retention.",
            "Server retention and omitted event types still require reconciliation.",
            "Current market metadata may not include historical or delisted markets.",
        ],
    }
    account_id = None
    for name, path, paginated in SOURCES:
        params = {"startTime": start_time_ms} if name == "funding" else {}
        if name in ("interest", "asset_operations_v2"):
            if account_id is None:
                manifest["sources"][name] = {"endpoint": path, "pages": [], "rows": 0,
                    "retrieval_complete": False, "error": "account_id_unavailable"}
                continue
            params = {"accountId": account_id}
            if name == "interest":
                params["interval"] = "ALL"
        result = _collect_resource(client, directory, name, path, paginated, params, max_pages)
        manifest["sources"][name] = result
        if name == "account" and result["retrieval_complete"]:
            info = _decode((directory / result["pages"][0]["file"]).read_text())
            value = info["data"].get("accountId") if isinstance(info["data"], dict) else None
            if isinstance(value, (int, str)) and not isinstance(value, bool):
                account_id = value
                manifest["account_id"] = value
    manifest["retrieval_complete"] = all(x["retrieval_complete"] for x in manifest["sources"].values())
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_private(directory / "manifest.json", json.dumps(manifest, indent=2) + "\n")
    return manifest
