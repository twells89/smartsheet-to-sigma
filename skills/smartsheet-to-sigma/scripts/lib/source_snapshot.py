"""Canonical Smartsheet source capture and identity."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import os
from pathlib import Path
from urllib.parse import urlparse

try:
    from .smartsheet_client import (
        SmartsheetApiError,
        SmartsheetClient,
    )
except ImportError:
    from smartsheet_client import SmartsheetApiError, SmartsheetClient


SCHEMA_VERSION = 1
ASSET_TYPES = {"sheet", "report", "dashboard"}
_ID_RE = re.compile(r"^\d+$")


class SnapshotError(RuntimeError):
    """A snapshot is malformed or cannot be captured safely."""


def parse_source(value: str, *, scope: str, asset_type: str | None = None) -> dict:
    """Parse a Smartsheet ID/URL into a source descriptor."""
    text = str(value).strip()
    if not text:
        raise SnapshotError("source ID or URL is required")
    source_type = "workspace" if scope == "workspace" else asset_type
    source_id = text if _ID_RE.fullmatch(text) else None
    if not source_id:
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or (
            host != "smartsheet.com"
            and not host.endswith(".smartsheet.com")
        ):
            raise SnapshotError("source URL must use HTTPS on a Smartsheet host")
        segments = [segment for segment in parsed.path.split("/") if segment]
        markers = {
            "sheets": "sheet",
            "reports": "report",
            "dashboards": "dashboard",
            "sights": "dashboard",
            "workspaces": "workspace",
        }
        for index, segment in enumerate(segments[:-1]):
            inferred = markers.get(segment.lower())
            candidate = segments[index + 1]
            if inferred and _ID_RE.fullmatch(candidate):
                source_type = inferred
                source_id = candidate
                break
        if not source_id:
            numeric = next((segment for segment in reversed(segments) if _ID_RE.fullmatch(segment)), None)
            source_id = numeric
    if not source_id:
        raise SnapshotError("could not determine a numeric Smartsheet ID from source")
    if scope == "workspace" and source_type != "workspace":
        raise SnapshotError("workspace scope requires a workspace ID or URL")
    if scope == "asset":
        if source_type not in ASSET_TYPES:
            raise SnapshotError(
                "asset type is ambiguous; pass --asset-type sheet, report, or dashboard"
            )
    normalized_url = None
    if "://" in text:
        parsed = urlparse(text)
        normalized_url = f"https://{parsed.hostname}{parsed.path}"
    return {"id": source_id, "type": source_type, "url": normalized_url}


def capture_snapshot(client: SmartsheetClient, source: dict, *, scope: str) -> dict:
    """Capture one asset or all accessible assets in a workspace."""
    assets: list[dict] = []
    errors: list[dict] = []
    if scope == "asset":
        assets, errors = _capture_asset_graph(
            client,
            source["type"],
            source["id"],
        )
    else:
        workspace = client.get_workspace(source["id"])
        shares, shares_error = client.optional_token_list(
            "/shares",
            params={"assetType": "workspace", "assetId": source["id"]},
            collection_key="items",
        )
        source = {
            **source,
            "name": workspace.get("name"),
            "raw": {
                key: workspace.get(key)
                for key in (
                    "id",
                    "name",
                    "accessLevel",
                    "permalink",
                    "createdAt",
                    "modifiedAt",
                )
                if key in workspace
            },
        }
        source["raw"]["_shares"] = shares
        if shares_error:
            source["raw"]["_shareError"] = shares_error
        seen: set[tuple[str, str]] = set()
        summaries = client.list_workspace_tree(
            source["id"],
            str(workspace.get("name") or source["id"]),
        )
        for summary in summaries:
            asset_type = summary["assetType"]
            asset_id = str(summary["id"])
            key = (asset_type, asset_id)
            if key in seen:
                continue
            seen.add(key)
            try:
                assets.append(
                    _capture_asset(
                        client,
                        asset_type,
                        asset_id,
                        path=str(summary.get("path") or ""),
                        inventory=summary,
                    )
                )
            except SmartsheetApiError as error:
                errors.append(
                    {
                        "code": "ASSET_CAPTURE_FAILED",
                        "assetType": asset_type,
                        "assetId": asset_id,
                        "path": summary.get("path") or "",
                        "status": error.status,
                        "message": str(error),
                    }
                )
    dependencies: list[dict] = []
    for asset in assets:
        dependencies.extend(_asset_dependencies(asset))
    snapshot = {
        "schemaVersion": SCHEMA_VERSION,
        "captureKind": "smartsheet",
        "capturedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "scope": scope,
        "source": source,
        "assets": sorted(assets, key=lambda item: (_type_order(item["type"]), item["id"])),
        "dependencies": _dedupe_dicts(dependencies),
        "errors": errors,
    }
    return finalize_snapshot(snapshot)


def _capture_asset_graph(
    client: SmartsheetClient,
    root_type: str,
    root_id: str,
) -> tuple[list[dict], list[dict]]:
    queue = [(root_type, str(root_id), "")]
    seen: set[tuple[str, str]] = set()
    expanded_workspaces: set[str] = set()
    assets: list[dict] = []
    errors: list[dict] = []
    while queue:
        asset_type, asset_id, path = queue.pop(0)
        key = (asset_type, asset_id)
        if key in seen:
            continue
        seen.add(key)
        try:
            asset = _capture_asset(client, asset_type, asset_id, path=path)
        except SmartsheetApiError as error:
            errors.append(
                {
                    "code": "ASSET_CAPTURE_FAILED",
                    "assetType": asset_type,
                    "assetId": asset_id,
                    "path": path,
                    "status": error.status,
                    "message": str(error),
                }
            )
            continue
        assets.append(asset)
        for dependency in _asset_dependencies(asset):
            target_type = dependency.get("targetType")
            if target_type in ASSET_TYPES:
                queue.append((target_type, dependency["to"], "dependency"))
            elif target_type == "workspace" and dependency["to"] not in expanded_workspaces:
                workspace_id = dependency["to"]
                expanded_workspaces.add(workspace_id)
                try:
                    metadata = client.get_workspace(workspace_id)
                    summaries = client.list_workspace_tree(
                        workspace_id,
                        str(metadata.get("name") or workspace_id),
                    )
                    for summary in summaries:
                        if summary.get("assetType") == "sheet":
                            queue.append(
                                (
                                    "sheet",
                                    str(summary["id"]),
                                    str(summary.get("path") or "dependency"),
                                )
                            )
                except SmartsheetApiError as error:
                    errors.append(
                        {
                            "code": "ASSET_CAPTURE_FAILED",
                            "assetType": "workspace",
                            "assetId": workspace_id,
                            "path": "report-scope",
                            "status": error.status,
                            "message": str(error),
                        }
                    )
    return assets, errors


def _capture_asset(
    client: SmartsheetClient,
    asset_type: str,
    asset_id: str,
    *,
    path: str,
    inventory: dict | None = None,
) -> dict:
    if asset_type == "sheet":
        raw = client.get_sheet(asset_id)
        automation, automation_error = client.optional_list(
            f"/sheets/{asset_id}/automationrules"
        )
        discussions, discussion_error = client.optional_list(
            f"/sheets/{asset_id}/discussions",
            params={"include": "comments,attachments"},
        )
        raw["_automationRules"] = automation
        raw["_discussionThreads"] = discussions
        feature_errors = [
            item
            for item in (
                automation_error,
                discussion_error,
                raw.get("_summaryError"),
            )
            if item is not None
        ]
    elif asset_type == "report":
        raw = client.get_report(asset_id)
        raw["_discussionCommentsUnavailable"] = bool(raw.get("discussions"))
        feature_errors = []
    elif asset_type == "dashboard":
        raw = client.get_dashboard(asset_id)
        feature_errors = []
    else:
        raise SnapshotError(f"unsupported asset type: {asset_type}")
    shares, shares_error = client.optional_token_list(
        "/shares",
        params={
            "assetType": "sight" if asset_type == "dashboard" else asset_type,
            "assetId": asset_id,
        },
        collection_key="items",
    )
    raw["_shares"] = shares
    if shares_error:
        feature_errors.append(shares_error)
    raw = _sanitize_source_data(raw)
    return {
        "id": str(raw.get("id") or asset_id),
        "type": asset_type,
        "name": str(raw.get("name") or f"{asset_type}-{asset_id}"),
        "path": path,
        "inventory": _sanitize_source_data(inventory or {}),
        "raw": raw,
        "featureErrors": feature_errors,
    }


def _asset_dependencies(asset: dict) -> list[dict]:
    raw = asset.get("raw") or {}
    source_id = asset["id"]
    dependencies: list[dict] = []
    if asset["type"] == "sheet":
        for reference in raw.get("crossSheetReferences") or []:
            target = reference.get("sourceSheetId") or reference.get("sheetId")
            if target is not None:
                dependencies.append(
                    {
                        "from": source_id,
                        "to": str(target),
                        "kind": "cross-sheet-reference",
                        "targetType": "sheet",
                    }
                )
    elif asset["type"] == "report":
        scope = raw.get("_scope") or raw.get("scope") or []
        for item in scope:
            if not isinstance(item, dict):
                continue
            target = item.get("assetId") or item.get("id")
            target_type = str(item.get("assetType") or "sheet").lower()
            if target is not None:
                dependencies.append(
                    {
                        "from": source_id,
                        "to": str(target),
                        "kind": f"report-{target_type}-scope",
                        "targetType": target_type,
                    }
                )
        for sheet in raw.get("sourceSheets") or []:
            target = sheet.get("id") if isinstance(sheet, dict) else sheet
            if target is not None:
                dependencies.append(
                    {
                        "from": source_id,
                        "to": str(target),
                        "kind": "report-sheet-source",
                        "targetType": "sheet",
                    }
                )
    elif asset["type"] == "dashboard":
        for target_type, target in _dashboard_source_ids(raw):
            dependencies.append(
                {
                    "from": source_id,
                    "to": target,
                    "kind": f"dashboard-{target_type}",
                    "targetType": "report" if target_type == "report-source" else "sheet",
                }
            )
    return dependencies


def _dashboard_source_ids(value) -> set[tuple[str, str]]:
    """Extract data sources from widget contents, excluding navigation links."""
    result: set[tuple[str, str]] = set()
    widgets = value.get("widgets") if isinstance(value, dict) else None
    for widget in widgets or []:
        if not isinstance(widget, dict):
            continue
        contents = widget.get("contents")
        if not isinstance(contents, dict):
            contents = widget
        for key, target_type in (
            ("sheetId", "sheet-source"),
            ("sourceSheetId", "sheet-source"),
            ("reportId", "report-source"),
            ("sourceReportId", "report-source"),
        ):
            target = contents.get(key)
            if target is not None:
                result.add((target_type, str(target)))
    return result


def canonical_payload(snapshot: dict) -> dict:
    """Return deterministic source content used for identity."""
    payload = {
        key: copy.deepcopy(snapshot.get(key))
        for key in ("schemaVersion", "captureKind", "scope", "source", "assets", "dependencies", "errors")
    }
    payload = _normalize_unordered(payload)
    payload["assets"] = sorted(
        payload.get("assets") or [],
        key=lambda item: (_type_order(item.get("type")), str(item.get("id"))),
    )
    payload["dependencies"] = sorted(
        payload.get("dependencies") or [],
        key=lambda item: (
            str(item.get("from")),
            str(item.get("to")),
            str(item.get("kind")),
        ),
    )
    payload["errors"] = sorted(
        payload.get("errors") or [],
        key=lambda item: (
            str(item.get("assetType")),
            str(item.get("assetId")),
            str(item.get("code")),
        ),
    )
    if isinstance(payload.get("source"), dict):
        payload["source"].pop("url", None)
    return payload


def snapshot_sha256(snapshot: dict) -> str:
    encoded = json.dumps(
        canonical_payload(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def finalize_snapshot(snapshot: dict) -> dict:
    output = copy.deepcopy(snapshot)
    output.setdefault("schemaVersion", SCHEMA_VERSION)
    output.setdefault("captureKind", "smartsheet")
    output.setdefault("capturedAt", None)
    output.setdefault("assets", [])
    output.setdefault("dependencies", [])
    output.setdefault("errors", [])
    output["sourceSha256"] = snapshot_sha256(output)
    validate_snapshot(output)
    return output


def validate_snapshot(snapshot: dict) -> None:
    if not isinstance(snapshot, dict):
        raise SnapshotError("snapshot must be a JSON object")
    if snapshot.get("schemaVersion") != SCHEMA_VERSION:
        raise SnapshotError(f"snapshot schemaVersion must be {SCHEMA_VERSION}")
    if snapshot.get("captureKind") != "smartsheet":
        raise SnapshotError("snapshot captureKind must be smartsheet")
    if snapshot.get("scope") not in {"asset", "workspace"}:
        raise SnapshotError("snapshot scope must be asset or workspace")
    if not isinstance(snapshot.get("assets"), list):
        raise SnapshotError("snapshot assets must be an array")
    if not isinstance(snapshot.get("dependencies"), list):
        raise SnapshotError("snapshot dependencies must be an array")
    if not isinstance(snapshot.get("errors"), list) or any(
        not isinstance(item, dict) for item in snapshot.get("errors") or []
    ):
        raise SnapshotError("snapshot errors must be an array of objects")
    source = snapshot.get("source")
    if (
        not isinstance(source, dict)
        or source.get("id") is None
        or source.get("type") not in ASSET_TYPES | {"workspace"}
    ):
        raise SnapshotError("snapshot source identity is invalid")
    if snapshot["scope"] == "workspace" and source.get("type") != "workspace":
        raise SnapshotError("workspace snapshot source must be a workspace")
    if snapshot["scope"] == "asset" and source.get("type") not in ASSET_TYPES:
        raise SnapshotError("asset snapshot source type is invalid")
    identities: set[tuple[str, str]] = set()
    for asset in snapshot["assets"]:
        if (
            not isinstance(asset, dict)
            or asset.get("type") not in ASSET_TYPES
            or asset.get("id") is None
            or not isinstance(asset.get("name"), str)
            or not asset["name"].strip()
            or not isinstance(asset.get("raw"), dict)
        ):
            raise SnapshotError("snapshot contains an invalid asset record")
        raw_id = asset["raw"].get("id")
        if raw_id is None or str(raw_id) != str(asset["id"]):
            raise SnapshotError("snapshot asset ID does not match its raw source ID")
        if "inventory" in asset and not isinstance(asset["inventory"], dict):
            raise SnapshotError("snapshot asset inventory must be an object")
        identity = (asset["type"], str(asset["id"]))
        if identity in identities:
            raise SnapshotError("snapshot contains duplicate asset identities")
        identities.add(identity)
    if snapshot["scope"] == "asset" and (
        source["type"],
        str(source["id"]),
    ) not in identities:
        raise SnapshotError("asset snapshot does not contain its declared root asset")
    for dependency in snapshot.get("dependencies") or []:
        if (
            not isinstance(dependency, dict)
            or dependency.get("from") is None
            or dependency.get("to") is None
            or not isinstance(dependency.get("kind"), str)
        ):
            raise SnapshotError("snapshot contains an invalid dependency record")
    expected = snapshot_sha256(snapshot)
    recorded = snapshot.get("sourceSha256")
    if recorded is not None and recorded != expected:
        raise SnapshotError("snapshot sourceSha256 does not match canonical content")


def load_snapshot(path: str | Path) -> dict:
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SnapshotError(f"could not load snapshot: {error}") from error
    if "sourceSha256" not in loaded:
        return finalize_snapshot(loaded)
    validate_snapshot(loaded)
    return loaded


def write_json(path: str | Path, value: dict | list) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.parent.chmod(0o700)
    except OSError:
        pass
    destination.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    destination.chmod(0o600)


def _type_order(asset_type: str | None) -> int:
    return {"sheet": 0, "report": 1, "dashboard": 2}.get(asset_type, 9)


def _dedupe_dicts(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for item in items:
        key = json.dumps(item, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return sorted(
        result,
        key=lambda item: (
            str(item.get("from")),
            str(item.get("to")),
            str(item.get("kind")),
        ),
    )


def _sanitize_source_data(value, *, in_attachment: bool = False):
    """Remove temporary attachment URLs before persistence and hashing."""
    if isinstance(value, dict):
        temporary_attachment = in_attachment and (
            str(value.get("attachmentType") or "").upper() == "FILE"
            or "urlExpiresInMillis" in value
        )
        output = {}
        for key, item in value.items():
            if temporary_attachment and key in {
                "url",
                "attachmentUrl",
                "downloadUrl",
                "urlExpiresInMillis",
            }:
                continue
            output[key] = _sanitize_source_data(
                item,
                in_attachment=key == "attachments",
            )
        return output
    if isinstance(value, list):
        return [
            _sanitize_source_data(item, in_attachment=in_attachment)
            for item in value
        ]
    return value


def _normalize_unordered(value):
    if isinstance(value, dict):
        output = {key: _normalize_unordered(item) for key, item in value.items()}
        for key in (
            "_shares",
            "_automationRules",
            "crossSheetReferences",
            "sourceSheets",
            "_scope",
        ):
            items = output.get(key)
            if isinstance(items, list):
                output[key] = sorted(
                    items,
                    key=lambda item: json.dumps(
                        item,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                )
        return output
    if isinstance(value, list):
        return [_normalize_unordered(item) for item in value]
    return value
