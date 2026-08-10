"""Restless Python SDK.

Capture your API traffic and send it to Restless.

    import restless

    client = restless.Restless(os.environ["RESTLESS_KEY"])

    @client.setup
    def _(request):
        return {
            "api_key": client.mask(request.headers.get("authorization")),
            "owner": {
                "id": current_workspace_id(),
                "enrich": lambda owner_id: {"label": lookup_name(owner_id)},
            },
        }

    app.wsgi_app = client.wsgi(app.wsgi_app)

Conformance: implements spec version 1.0.0 of the Restless SDK Contract at
level L2. Run the shared harness against ``python -m restless._conformance``
to verify. See CONFORMANCE.md.
"""

import os
import sys
from typing import Any, Callable, Dict, List, Optional

from ._capture import CaptureEngine
from ._mask import mask
from ._request import Headers, RequestInfo
from ._request_id import (
    format_request_id,
    is_valid_request_id,
    new_request_id,
    strip_request_id_prefix,
)
from ._settings import load_settings, resolve_api
from ._uploader import is_test_run, resolve_base_url
from ._version import CONFORMANCE_LEVEL, SDK_NAME, SDK_VERSION, SPEC_VERSION

__all__ = [
    "Restless",
    "restless",
    "mask",
    "RequestInfo",
    "Headers",
    "new_request_id",
    "format_request_id",
    "strip_request_id_prefix",
    "is_valid_request_id",
    "SDK_NAME",
    "SDK_VERSION",
    "SPEC_VERSION",
    "CONFORMANCE_LEVEL",
]

__version__ = SDK_VERSION


class Restless:
    """A Restless client.

    Naming here is idiomatic Python rather than a transliteration of the Node
    SDK. CONTRACT.md section 14 makes the public surface explicitly
    per-language: what is normative is the SEMANTIC surface (construct with
    an explicit key, register a per-request callback, mask a key, flush), not
    the method names.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api: Optional[str] = None,
        redact: Optional[Dict[str, List[str]]] = None,
        base_url: Optional[str] = None,
        transport: Optional[Callable] = None,
    ):
        # CONFIG-001. Explicit key wins, then RESTLESS_KEY, then README_API_KEY.
        resolved = (
            api_key
            or os.environ.get("RESTLESS_KEY")
            or os.environ.get("README_API_KEY")
            or ""
        )
        # CONFIG-002. Capture still runs; only upload is disabled.
        if not resolved and not is_test_run():
            print(
                "[restless] no API key found - set RESTLESS_KEY in your environment "
                "or pass it to Restless(). Captured requests will not be uploaded.",
                file=sys.stderr,
            )

        request_id_prefix = None
        settings_redact = None
        entry = resolve_api(load_settings(), api)  # CONFIG-013/014 may raise
        if entry:
            request_id_prefix = entry.get("requestIdPrefix")
            settings_redact = entry.get("redact")

        # REDACT-015. Both sources are additive on top of the defaults.
        merged: Dict[str, List[str]] = {}
        for field in ("headers", "bodyKeys", "queryParams"):
            merged[field] = list((settings_redact or {}).get(field) or []) + list(
                (redact or {}).get(field) or []
            )

        self.engine = CaptureEngine(
            api_key=resolved,
            base_url=resolve_base_url(base_url),
            request_id_prefix=request_id_prefix,
            redact=merged,
            transport=transport,
        )

    # ------------------------------------------------------------ public API

    @staticmethod
    def mask(api_key: Optional[str]) -> Optional[str]:
        """Mask an end-user API key (MASK-001).

        Pass the raw header value straight through. Do NOT substitute a
        placeholder like ``"anonymous"``: its last 4 characters would become
        the tail and cluster unrelated callers together.
        """
        return mask(api_key)

    def setup(self, callback: Callable) -> Callable:
        """Register the per-request callback. Usable as a decorator.

        The callback receives the framework-native request object and returns
        a dict: ``api_key`` (masked), ``owner`` (``id`` plus ``enrich``), and
        optionally ``block``.
        """
        self.engine.set_callback(callback)
        return callback

    def flush(self) -> None:
        """Force-upload the queued batch. Call before process exit if you
        care about in-flight captures."""
        self.engine.flush()

    # ------------------------------------------------------------- adapters

    def wsgi(self, app: Callable) -> Callable:
        """Wrap a WSGI app (Flask, Django, Pyramid, Bottle)."""
        from .adapters.wsgi import wrap_wsgi

        return wrap_wsgi(app, self.engine)

    def asgi(self, app: Callable) -> Callable:
        """Wrap an ASGI app (FastAPI, Starlette, Quart)."""
        from .adapters.asgi import wrap_asgi

        return wrap_asgi(app, self.engine)


def restless(api_key: Optional[str] = None, **kwargs: Any) -> Restless:
    """Functional constructor, mirroring the Node SDK's ``restless(key)``."""
    return Restless(api_key, **kwargs)
