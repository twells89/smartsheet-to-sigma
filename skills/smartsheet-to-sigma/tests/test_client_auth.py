import io
import json
import os
import sys
from pathlib import Path
import unittest
import urllib.error
import urllib.parse
from unittest import mock


LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
sys.path.insert(0, str(LIB))

from smartsheet_auth import SmartsheetAuthError, resolve_access_token
from smartsheet_client import SmartsheetClient


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class ClientAuthTests(unittest.TestCase):
    def test_auth_uses_environment_without_echoing_token(self):
        token = "x" * 32
        with mock.patch.dict(os.environ, {"SMARTSHEET_ACCESS_TOKEN": token}, clear=True):
            self.assertEqual(resolve_access_token(), token)
            self.assertNotIn(token, repr(SmartsheetClient(token)))

    def test_conflicting_tokens_fail(self):
        with mock.patch.dict(
            os.environ,
            {
                "SMARTSHEET_ACCESS_TOKEN": "x" * 32,
                "SMARTSHEET_API_TOKEN": "y" * 32,
            },
            clear=True,
        ):
            with self.assertRaises(SmartsheetAuthError):
                resolve_access_token()

    def test_retries_rate_limit_using_retry_after(self):
        calls = []
        sleeps = []

        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "rate limited",
                    {"Retry-After": "0"},
                    io.BytesIO(b'{"message":"slow down"}'),
                )
            return Response({"id": 1})

        client = SmartsheetClient(
            "x" * 32,
            opener=opener,
            sleeper=sleeps.append,
            max_retries=1,
        )
        self.assertEqual(client.get("/sheets/1")["id"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [0.0])

    def test_list_all_follows_pages(self):
        def opener(request, timeout):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            page = int(query["page"][0])
            return Response(
                {
                    "data": [{"id": page}],
                    "pageNumber": page,
                    "totalPages": 2,
                }
            )

        client = SmartsheetClient("x" * 32, opener=opener)
        self.assertEqual(client.list_all("/sheets"), [{"id": 1}, {"id": 2}])

    def test_token_pagination_follows_last_key(self):
        def opener(request, timeout):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            if "lastKey" not in query:
                return Response({"data": [{"id": 1}], "lastKey": "next"})
            return Response({"data": [{"id": 2}]})

        client = SmartsheetClient("x" * 32, opener=opener)
        self.assertEqual(
            client.list_token_all("/workspaces/1/children"),
            [{"id": 1}, {"id": 2}],
        )

    def test_get_sheet_pages_all_rows_and_marks_complete(self):
        def opener(request, timeout):
            parsed = urllib.parse.urlparse(request.full_url)
            if parsed.path.endswith("/sheets/1/summary"):
                return Response({"fields": [{"id": 10, "title": "Total"}]})
            query = urllib.parse.parse_qs(parsed.query)
            page = int(query["page"][0])
            return Response(
                {
                    "id": 1,
                    "version": 5,
                    "totalRowCount": 2,
                    "columns": [],
                    "rows": [{"id": page}],
                }
            )

        client = SmartsheetClient("x" * 32, opener=opener)
        sheet = client.get_sheet("1")
        self.assertEqual([row["id"] for row in sheet["rows"]], [1, 2])
        self.assertTrue(sheet["_captureComplete"])
        self.assertEqual(sheet["summary"]["fields"][0]["title"], "Total")

    def test_get_report_retains_rows_when_source_version_is_inaccessible(self):
        def opener(request, timeout):
            parsed = urllib.parse.urlparse(request.full_url)
            path = parsed.path
            if path.endswith("/reports/2/scope"):
                return Response({"data": [{"assetType": "sheet", "assetId": 1}]})
            if path.endswith("/sheets/1/version"):
                raise urllib.error.HTTPError(
                    request.full_url,
                    403,
                    "forbidden",
                    {},
                    io.BytesIO(b'{"message":"forbidden"}'),
                )
            if path.endswith("/reports/2/definition"):
                return Response({"id": 2})
            if path.endswith("/reports/2"):
                return Response(
                    {
                        "id": 2,
                        "totalRowCount": 1,
                        "rows": [{"id": 20}],
                    }
                )
            raise AssertionError(path)

        client = SmartsheetClient("x" * 32, opener=opener)
        report = client.get_report("2")
        self.assertEqual(report["rows"], [{"id": 20}])
        self.assertFalse(report["_consistencyVerified"])
        self.assertFalse(report["_captureComplete"])
        self.assertEqual(len(report["_versionErrors"]), 2)

    def test_workspace_tree_uses_current_children_endpoints(self):
        calls = []

        def opener(request, timeout):
            path = urllib.parse.urlparse(request.full_url).path
            calls.append(path)
            if path.endswith("/workspaces/1/children"):
                return Response(
                    {
                        "data": [
                            {"id": 2, "name": "Inputs", "resourceType": "folder"},
                            {"id": 3, "name": "Sheet", "resourceType": "sheet"},
                        ]
                    }
                )
            if path.endswith("/folders/2/children"):
                return Response(
                    {
                        "data": [
                            {"id": 4, "name": "Report", "resourceType": "report"}
                        ]
                    }
                )
            raise AssertionError(path)

        client = SmartsheetClient("x" * 32, opener=opener)
        assets = client.list_workspace_tree("1", "Workspace")
        self.assertEqual(
            [(item["assetType"], item["path"]) for item in assets],
            [("report", "Workspace/Inputs"), ("sheet", "Workspace")],
        )
        self.assertIn("/2.0/workspaces/1/children", calls)
        self.assertIn("/2.0/folders/2/children", calls)

    def test_dashboard_uses_sights_endpoint_and_level_four(self):
        captured = {}

        def opener(request, timeout):
            captured["url"] = request.full_url
            return Response({"id": 7, "name": "Dashboard", "widgets": []})

        client = SmartsheetClient("x" * 32, opener=opener)
        client.get_dashboard("7")
        parsed = urllib.parse.urlparse(captured["url"])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/2.0/sights/7")
        self.assertEqual(query["level"], ["4"])
        self.assertEqual(query["include"], ["objectValue,source"])


if __name__ == "__main__":
    unittest.main()
