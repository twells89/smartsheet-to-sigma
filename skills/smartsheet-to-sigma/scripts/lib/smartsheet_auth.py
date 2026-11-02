"""Resolve Smartsheet read credentials without accepting secrets on argv."""
from __future__ import annotations

import os
import re


class SmartsheetAuthError(RuntimeError):
    """A usable Smartsheet access token was not available."""


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def resolve_access_token() -> str:
    """Return an OAuth/PAT bearer token from the environment.

    ``SMARTSHEET_ACCESS_TOKEN`` is canonical. ``SMARTSHEET_API_TOKEN`` remains a
    compatibility alias. The value is deliberately never included in errors or
    object representations.
    """
    primary = os.environ.get("SMARTSHEET_ACCESS_TOKEN")
    compatibility = os.environ.get("SMARTSHEET_API_TOKEN")
    if primary and compatibility and primary != compatibility:
        raise SmartsheetAuthError(
            "SMARTSHEET_ACCESS_TOKEN and SMARTSHEET_API_TOKEN differ; unset one"
        )
    token = primary or compatibility
    if not token:
        raise SmartsheetAuthError(
            "set SMARTSHEET_ACCESS_TOKEN to a read-capable OAuth access token "
            "or personal access token"
        )
    token = token.strip()
    if len(token) < 16 or _CONTROL_RE.search(token):
        raise SmartsheetAuthError("Smartsheet access token has an invalid shape")
    return token
