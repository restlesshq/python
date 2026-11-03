"""The read-only request view handed to the setup callback.

Cross-SDK parity, not a Python invention: Ruby has
``Restless::Rack::RequestInfo`` (``request.header("Authorization")``,
``request_method``, ``path``, ``query_string``, ``url``, raw ``env``) and Go
has ``*restless.RequestInfo`` (``r.Header("Authorization")``, ``r.Request``,
``r.Route``). Python was the only SDK handing the callback a raw protocol
dict, which meant the documented ``request.headers.get(...)`` raised
``AttributeError`` on a dict, and SAFETY-002 swallowed it: the install looked
healthy while attributing nothing to anyone.

CONTRACT.md section 14 makes the callback's ARGUMENT non-normative (only the
returned shape in section 15 is fixed), so each SDK is free to choose here.
This picks parity, because "the same concept is spelled a bit differently per
language" is the intended kind of difference and "one SDK silently drops your
owner" is not.

One class covers both protocols. Ruby and Go each speak a single one; Python
has WSGI and ASGI, and a callback that works under one and breaks under the
other would recreate the original problem in a smaller shape.
"""

import re
from typing import Any, Dict, Iterator, Mapping, Optional

from ._fingerprint import _WS_CHARS


class Headers(Mapping):
    """Case-insensitive read-only header mapping.

    The adapters already normalize to lowercase, so this exists for the
    caller's benefit: ``request.headers["Authorization"]`` and
    ``request.headers.get("authorization")`` must not disagree. Getting that
    wrong returns ``None`` for a header that is present, which is exactly the
    class of silent failure this module was added to remove.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Optional[Dict[str, str]] = None):
        self._data = data or {}

    def __getitem__(self, name: str) -> str:
        return self._data[str(name).lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, name: str, default: Any = None) -> Any:
        return self._data.get(str(name).lower(), default)

    def __contains__(self, name: object) -> bool:
        return str(name).lower() in self._data

    def __repr__(self) -> str:
        return "Headers({!r})".format(self._data)


class RequestInfo:
    """Read-only view of the request in progress.

    Attributes:
        headers: case-insensitive mapping of request headers.
        method:  the HTTP method.
        url:     the full URL, as the capture records it.
        path:    the request path.
        query_string: the raw query string, without ``?``.
        environ: the WSGI environ, or ``None`` under ASGI.
        scope:   the ASGI scope, or ``None`` under WSGI.
        raw:     whichever of the two is set.
        route:   the templated route, when it is already known. See below.
        framework_request: whatever an upstream layer put in
            ``environ["restless.request"]``, or ``None``.
    """

    __slots__ = (
        "headers", "method", "url", "route", "environ", "scope", "framework_request",
    )

    def __init__(
        self,
        headers: Optional[Dict[str, str]] = None,
        method: str = "GET",
        url: str = "",
        route: Optional[str] = None,
        environ: Optional[Dict[str, Any]] = None,
        scope: Optional[Dict[str, Any]] = None,
    ):
        self.headers = Headers(headers)
        self.method = method
        self.url = url
        # Usually None here. The setup callback runs BEFORE the application,
        # so the router has not matched yet and neither Flask's `url_rule` nor
        # Starlette's `scope["route"]` exists. It is populated only when
        # something upstream already set `environ["restless.route"]`. The
        # captured log still gets the real route: the adapter reads it again
        # after the response, where it is known.
        self.route = route
        self.environ = environ
        self.scope = scope
        self.framework_request = (environ or {}).get("restless.request")

    def header(self, name: str) -> Optional[str]:
        """One header, case-insensitively. Mirrors Ruby's ``header`` and Go's ``Header``."""
        return self.headers.get(name)

    def __getitem__(self, name: str) -> Optional[str]:
        """``request["authorization"]``, mirroring Ruby's ``[]`` alias."""
        return self.headers.get(name)

    @property
    def raw(self) -> Dict[str, Any]:
        """The underlying protocol dict, whichever this is."""
        return self.environ if self.environ is not None else (self.scope or {})

    @property
    def path(self) -> str:
        if self.environ is not None:
            # SCRIPT_NAME + PATH_INFO, matching Ruby's `path` and giving the
            # externally visible path for an app mounted under a prefix.
            return "{}{}".format(
                self.environ.get("SCRIPT_NAME", ""), self.environ.get("PATH_INFO", "")
            )
        scope = self.scope or {}
        return "{}{}".format(scope.get("root_path", ""), scope.get("path", ""))

    @property
    def query_string(self) -> str:
        if self.environ is not None:
            return self.environ.get("QUERY_STRING", "") or ""
        raw = (self.scope or {}).get("query_string") or b""
        return raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)

    def __repr__(self) -> str:
        return "RequestInfo({} {})".format(self.method, self.path)


_QUOTED = re.compile(r'"(.*)"')


def _header_value(headers: Mapping[str, str], name: str) -> Optional[str]:
    for key, value in headers.items():
        if key.lower() == name and value:
            return value
    return None


def _valid_scheme(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    # PRIM-002: strip the JS `trim()` set, not Python's `str.strip()` set.
    s = raw.strip(_WS_CHARS)
    quoted = _QUOTED.fullmatch(s)  # PRIM-005
    if quoted:
        s = quoted.group(1)
    s = s.lower()
    return s if s in ("http", "https") else None


def request_scheme(headers: Mapping[str, str], transport_scheme: str) -> str:
    """The scheme the client used, for the logged URL (CONTRACT.md HAR-013).

    A TLS-terminating proxy (Render, Heroku, an ALB, nginx) hands the app
    plain HTTP, so ``wsgi.url_scheme`` / ``scope["scheme"]`` alone log every
    request as ``http://``. gunicorn and uvicorn only correct those from
    forwarded headers for a configured list of proxy IPs, which a typical
    PaaS deploy never sets. The first hop's ``Forwarded: proto=`` wins,
    then the first ``X-Forwarded-Proto`` value, then the transport. Only
    ``http`` and ``https`` are accepted from a header. The headers are
    trusted without a proxy list: a spoofed value only mislabels the
    sender's own log entry.
    """
    forwarded = _header_value(headers, "forwarded")
    if forwarded:
        for pair in forwarded.split(",")[0].split(";"):
            name, eq, value = pair.partition("=")
            if not eq or name.strip(_WS_CHARS).lower() != "proto":
                continue
            scheme = _valid_scheme(value)
            if scheme:
                return scheme
    xfp = _header_value(headers, "x-forwarded-proto")
    scheme = _valid_scheme(xfp.split(",")[0] if xfp else None)
    if scheme:
        return scheme
    return transport_scheme
