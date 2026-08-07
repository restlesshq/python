"""`.restless/settings.json` discovery.

Implements CONTRACT.md section 12.
"""

import json
import os
from typing import Any, Dict, Optional

_cached: Any = ...  # sentinel: not yet loaded


def find_settings_file(start_dir: Optional[str] = None) -> Optional[str]:
    """CONFIG-010. Walk up to the filesystem root, first hit wins."""
    directory = os.path.abspath(start_dir or os.getcwd())
    while True:
        candidate = os.path.join(directory, ".restless", "settings.json")
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def load_settings(start_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """CONFIG-011, CONFIG-012. Read once per process, negative result cached."""
    global _cached
    if _cached is not ...:
        return _cached
    path = find_settings_file(start_dir)
    if not path:
        _cached = None
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            _cached = json.load(handle)
    except (OSError, ValueError):
        # A malformed file must not prevent construction.
        _cached = None
    return _cached


def _reset_cache() -> None:
    """Test-only."""
    global _cached
    _cached = ...


def resolve_api(
    settings: Optional[Dict[str, Any]], name: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """CONFIG-013, CONFIG-014, CONFIG-015."""
    if not settings:
        return None
    apis = settings.get("apis") or []
    if not apis:
        return None

    if name:
        match = next((a for a in apis if a.get("name") == name), None)
        if match is None:
            match = next((a for a in apis if a.get("id") == name), None)
        if match is None:
            raise ValueError(
                'restless: no API named "{}" in .restless/settings.json (found: {})'.format(
                    name, ", ".join(str(a.get("name")) for a in apis)
                )
            )
        return _entry(match)

    if len(apis) == 1:
        return _entry(apis[0])

    # Guessing would silently apply the wrong redaction list.
    raise ValueError(
        "restless: .restless/settings.json has multiple APIs ({}) - pass "
        'api="<name>" to restless() to pick one.'.format(
            ", ".join(str(a.get("name")) for a in apis)
        )
    )


def _entry(api: Dict[str, Any]) -> Dict[str, Any]:
    # CONFIG-015: only these two fields are consumed at runtime.
    return {
        "id": api.get("id"),
        "name": api.get("name"),
        "requestIdPrefix": api.get("requestIdPrefix"),
        "redact": api.get("redact"),
    }
