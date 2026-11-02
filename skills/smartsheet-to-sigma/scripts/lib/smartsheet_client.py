"""Small read-only Smartsheet API client used by source capture."""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable


class SmartsheetApiError(RuntimeError):
    """A Smartsheet API read failed."""

    def __init__(self, message: str, *, status: int | None = None, path: str = ""):
        super().__init__(message)
        self.status = status
        self.path = path


class SmartsheetClient:
    """Read-only JSON client with bounded rate-limit and transient retries."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.smartsheet.com/2.0",
        timeout: float = 60,
        max_retries: int = 4,
        opener: Callable | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleeper

    def __repr__(self) -> str:
        return f"SmartsheetClient(base_url={self.base_url!r})"

    def get(self, path: str, params: dict | None = None) -> dict:
        """GET one JSON object, retrying 429 and transient 5xx responses."""
        if not path.startswith("/"):
            path = "/" + path
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = self.base_url + path + (("?" + query) if query else "")
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "smartsheet-to-sigma/0.1",
            },
            method="GET",
        )
        for attempt in range(self.max_retries + 1):
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    payload = response.read()
                decoded = json.loads(payload.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise SmartsheetApiError("Smartsheet returned non-object JSON", path=path)
                return decoded
            except urllib.error.HTTPError as error:
                retryable = error.code == 429 or 500 <= error.code < 600
                if retryable and attempt < self.max_retries:
                    retry_after = error.headers.get("Retry-After") if error.headers else None
                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        delay = min(2**attempt, 16) + random.random() / 4
                    self._sleep(max(0.0, delay))
                    continue
                message = _http_error_message(error)
                raise SmartsheetApiError(
                    f"Smartsheet GET {path} failed ({error.code}): {message}",
                    status=error.code,
                    path=path,
                ) from error
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                if attempt < self.max_retries:
                    self._sleep(min(2**attempt, 16) + random.random() / 4)
                    continue
                raise SmartsheetApiError(
                    f"Smartsheet GET {path} failed after retries: {type(error).__name__}",
                    path=path,
                ) from error
        raise AssertionError("unreachable")

    def list_all(
        self,
        path: str,
        *,
        params: dict | None = None,
        page_size: int = 100,
    ) -> list:
        """Collect a standard Smartsheet paged ``data`` response."""
        base = dict(params or {})
        page = 1
        values: list = []
        while True:
            payload = self.get(path, {**base, "page": page, "pageSize": page_size})
            data = payload.get("data")
            if not isinstance(data, list):
                raise SmartsheetApiError(
                    f"Smartsheet GET {path} did not return a data array", path=path
                )
            values.extend(data)
            total_pages = int(payload.get("totalPages") or page)
            if page >= total_pages:
                return values
            page += 1

    def list_token_all(
        self,
        path: str,
        *,
        params: dict | None = None,
        collection_key: str = "data",
        max_items: int = 1000,
    ) -> list:
        """Collect a current Smartsheet ``lastKey``-paginated response."""
        base = dict(params or {})
        last_key = None
        values: list = []
        seen_keys: set[str] = set()
        while True:
            request_params = {**base, "maxItems": max_items}
            if last_key:
                request_params["lastKey"] = last_key
            payload = self.get(path, request_params)
            data = payload.get(collection_key)
            if not isinstance(data, list):
                raise SmartsheetApiError(
                    f"Smartsheet GET {path} did not return a {collection_key} array",
                    path=path,
                )
            values.extend(data)
            next_key = payload.get("lastKey")
            if not next_key:
                return values
            next_key = str(next_key)
            if next_key in seen_keys:
                raise SmartsheetApiError(
                    f"Smartsheet GET {path} repeated pagination token", path=path
                )
            seen_keys.add(next_key)
            last_key = next_key

    def get_sheet(self, sheet_id: str) -> dict:
        params = {
            "accessApiLevel": 1,
            "include": (
                "attachments,columnType,crossSheetReferences,discussions,"
                "filterDefinitions,format,ganttConfig,objectValue,ownerInfo,"
                "proofs,rowPermalink,source,writerInfo"
            ),
            "level": 2,
            "pageSize": 5000,
        }
        first = self.get(f"/sheets/{sheet_id}", {**params, "page": 1})
        sheet = self._merge_row_pages(
            f"/sheets/{sheet_id}",
            first,
            params,
            total_key="totalRowCount",
        )
        try:
            sheet["summary"] = self.get(f"/sheets/{sheet_id}/summary")
        except SmartsheetApiError as error:
            sheet["_summaryError"] = {
                "path": error.path,
                "status": error.status,
                "error": str(error),
            }
        return sheet

    def get_report(self, report_id: str) -> dict:
        """Fetch a report and merge all row pages (10,000 rows per request)."""
        scope = self.list_token_all(f"/reports/{report_id}/scope")
        direct_sheet_ids = [
            str(item["assetId"])
            for item in scope
            if str(item.get("assetType") or "").lower() == "sheet"
            and item.get("assetId") is not None
        ]
        has_workspace_scope = any(
            str(item.get("assetType") or "").lower() == "workspace"
            for item in scope
        )
        versions_before, version_errors = self._sheet_versions(direct_sheet_ids)
        params = {
            "accessApiLevel": 1,
            "include": (
                "attachments,discussions,proofs,format,objectValue,scope,source,"
                "sourceSheets"
            ),
            "level": 3,
            "pageSize": 10000,
        }
        first = self.get(f"/reports/{report_id}", {**params, "page": 1})
        first = self._merge_row_pages(
            f"/reports/{report_id}",
            first,
            params,
            total_key="totalRowCount",
        )
        first["_definition"] = self.get(f"/reports/{report_id}/definition")
        first["_scope"] = scope
        versions_after, errors_after = self._sheet_versions(direct_sheet_ids)
        version_errors.extend(errors_after)
        first["_consistencyVerified"] = (
            not has_workspace_scope
            and not version_errors
            and versions_before == versions_after
        )
        if version_errors:
            first["_versionErrors"] = version_errors
        if version_errors or versions_before != versions_after:
            first["_captureComplete"] = False
        return first

    def get_dashboard(self, dashboard_id: str) -> dict:
        return self.get(
            f"/sights/{dashboard_id}",
            {
                "accessApiLevel": 1,
                "include": "objectValue,source",
                "level": 4,
            },
        )

    def get_workspace(self, workspace_id: str) -> dict:
        return self.get(
            f"/workspaces/{workspace_id}/metadata",
            {"accessApiLevel": 1},
        )

    def list_workspace_tree(self, workspace_id: str, workspace_name: str) -> list[dict]:
        """Return flattened, path-aware assets using current children endpoints."""
        result: list[dict] = []
        visited_folders: set[str] = set()

        def visit(path: str, container_type: str, container_id: str):
            if container_type == "folder":
                if container_id in visited_folders:
                    return
                visited_folders.add(container_id)
            endpoint = (
                f"/workspaces/{container_id}/children"
                if container_type == "workspace"
                else f"/folders/{container_id}/children"
            )
            children = self.list_token_all(
                endpoint,
                params={
                    "childrenResourceTypes": "sheets,reports,sights,folders",
                    "include": "source,ownerInfo",
                    "accessApiLevel": 1,
                },
            )
            for child in children:
                resource_type = str(child.get("resourceType") or "").lower()
                if resource_type == "folder":
                    name = str(child.get("name") or child.get("id") or "folder")
                    visit(f"{path}/{name}".strip("/"), "folder", str(child["id"]))
                elif resource_type in {"sheet", "report", "sight"}:
                    result.append(
                        {
                            **child,
                            "assetType": "dashboard" if resource_type == "sight" else resource_type,
                            "path": path,
                        }
                    )

        visit(workspace_name, "workspace", str(workspace_id))
        return result

    def list_asset_shares(self, asset_type: str, asset_id: str) -> list:
        api_type = "sight" if asset_type == "dashboard" else asset_type
        return self.list_token_all(
            "/shares",
            params={"assetType": api_type, "assetId": str(asset_id)},
            collection_key="items",
        )

    def optional_list(
        self, path: str, *, params: dict | None = None
    ) -> tuple[list, dict | None]:
        """Read an optional feature and retain a safe error record on failure."""
        try:
            return self.list_all(path, params=params), None
        except SmartsheetApiError as error:
            return [], {
                "path": path,
                "status": error.status,
                "error": str(error),
            }

    def optional_token_list(
        self,
        path: str,
        *,
        params: dict | None = None,
        collection_key: str = "data",
    ) -> tuple[list, dict | None]:
        try:
            return self.list_token_all(
                path,
                params=params,
                collection_key=collection_key,
            ), None
        except SmartsheetApiError as error:
            return [], {
                "path": path,
                "status": error.status,
                "error": str(error),
            }

    def _sheet_versions(self, sheet_ids: list[str]) -> tuple[dict, list[dict]]:
        versions = {}
        errors = []
        for sheet_id in sheet_ids:
            path = f"/sheets/{sheet_id}/version"
            try:
                versions[sheet_id] = self.get(path).get("version")
            except SmartsheetApiError as error:
                errors.append(
                    {
                        "path": path,
                        "status": error.status,
                        "error": str(error),
                    }
                )
        return versions, errors

    def _merge_row_pages(
        self,
        path: str,
        first: dict,
        params: dict,
        *,
        total_key: str,
    ) -> dict:
        rows_by_id = {
            str(row.get("id")): row
            for row in first.get("rows") or []
            if row.get("id") is not None
        }
        total = int(first.get(total_key) or len(rows_by_id))
        version = first.get("version")
        page = 2
        consistent = True
        while len(rows_by_id) < total:
            payload = self.get(path, {**params, "page": page})
            if version is not None and payload.get("version") not in (None, version):
                consistent = False
            batch = payload.get("rows") or []
            before = len(rows_by_id)
            for row in batch:
                if row.get("id") is not None:
                    rows_by_id[str(row["id"])] = row
            if not batch or len(rows_by_id) == before:
                consistent = False
                break
            page += 1
        first["rows"] = list(rows_by_id.values())
        first["_captureComplete"] = consistent and len(rows_by_id) == total
        return first


def _http_error_message(error: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
    except Exception:
        return error.reason or "HTTP error"
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("errorCode") or "API error")
    return "API error"
