"""Sampling: per-endpoint control over how much of each request is sent.

Implements CONTRACT.md section 12.1 (``SAMPLE``).

Everything here is pure. The engine draws ``u`` (``random.random()``) once
per request and passes it to ``decide``; tests and the conformance driver
pass a fixed one.

A mode is ``"on"`` (everything in full), ``"off"`` (nothing), ``"errors"``
(only status >= 400) or a rate in ``[0, 1]``: that fraction of successful
requests in full and the rest as bodyless stubs. Errors are always sent in
full, except under ``"off"``.
"""

import hashlib
import math
import re
from typing import Any, Dict, List, Optional, Tuple, Union

Mode = Union[str, float]

FULL = "full"
STUB = "stub"
DROP = "drop"

#: SAMPLE-002. Rates are quantized to this many steps.
_RATE_STEPS = 10000

_STRING_MODES = ("on", "off", "errors")

# SAMPLE-010. Printable ASCII only, so every SDK sorts and hashes keys
# byte for byte the same. Both are used with fullmatch: Python's `$` would
# also match before a trailing newline (PRIM-005).
_PATH_RE = re.compile(r"/[\x21-\x7e]*", re.ASCII)
_METHOD_RE = re.compile(r"[A-Za-z]+|\*", re.ASCII)
# SAMPLE-011. An explicit ASCII class, not `\w`, which is Unicode-aware here.
_PARAM_RE = re.compile(r":([A-Za-z0-9_]+)", re.ASCII)

_ASCII_UPPER = {c: c - 32 for c in range(ord("a"), ord("z") + 1)}


class SamplingConfig(object):
    """A validated, merged configuration. Rates are already quantized."""

    __slots__ = ("default", "routes")

    def __init__(self, default: Mode = "on", routes: Optional[Dict[str, Mode]] = None):
        self.default = default
        self.routes = routes if routes is not None else {}

    @property
    def is_default(self) -> bool:
        """SAMPLE-041. All "on" with no routes: exactly no sampling."""
        return self.default == "on" and not self.routes


def _is_number(value: Any) -> bool:
    # bool is an int subclass in Python, but JSON `true` is not a rate.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalize_mode(value: Any) -> Optional[Mode]:
    """SAMPLE-001..003. The normalized mode, or None when ``value`` is not a
    valid mode (the caller decides what invalid falls back to)."""
    if isinstance(value, str):
        return value if value in _STRING_MODES else None
    if not _is_number(value):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or number > 1:
        return None
    # SAMPLE-002. floor(x + 0.5), NOT round(): Python's round() is half to even.
    steps = math.floor(number * _RATE_STEPS + 0.5)
    # A rate that rounds to 1 IS "on"; one spelling, so the hash never sees two.
    return "on" if steps >= _RATE_STEPS else steps / _RATE_STEPS


def normalize_path(path: str) -> Optional[str]:
    """SAMPLE-011. ``/pets/:id/`` becomes ``/pets/{id}``; None if invalid."""
    p = _PARAM_RE.sub(r"{\1}", path)
    while len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    return p if _PATH_RE.fullmatch(p) else None


def normalize_route_key(key: str) -> Optional[str]:
    """SAMPLE-010. ``"get  /pets/:id/"`` becomes ``"GET /pets/{id}"``."""
    trimmed = key.strip(" ")
    space = trimmed.find(" ")
    if space == -1:
        return None
    method = trimmed[:space]
    path = normalize_path(trimmed[space:].lstrip(" "))
    if path is None or not _METHOD_RE.fullmatch(method):
        return None
    return "{} {}".format(_ascii_upper(method), path)


def normalize_sampling(settings: Any = None, code: Any = None) -> Tuple[SamplingConfig, List[str]]:
    """SAMPLE-013, SAMPLE-014. Merge the settings-file and code configs and
    validate every value. Never raises: a bad mode degrades to "on", a bad
    key is ignored, and both are reported in the returned warnings."""
    warnings: List[str] = []
    config = SamplingConfig()

    for source, raw in (("settings", settings), ("code", code)):
        if raw is None:
            continue
        if not isinstance(raw, dict):
            warnings.append("sampling ({}): expected an object, ignored".format(source))
            continue

        if "default" in raw:
            mode = normalize_mode(raw["default"])
            if mode is None:
                warnings.append(
                    'sampling.default ({}): invalid mode {!r}, using "on"'.format(
                        source, raw["default"]
                    )
                )
            config.default = "on" if mode is None else mode

        routes = raw.get("routes")
        if routes is None:
            continue
        if not isinstance(routes, dict):
            warnings.append("sampling.routes ({}): expected an object, ignored".format(source))
            continue
        for raw_key, raw_mode in routes.items():
            key = normalize_route_key(raw_key) if isinstance(raw_key, str) else None
            if key is None:
                warnings.append(
                    "sampling.routes ({}): invalid route key {!r}, ignored".format(source, raw_key)
                )
                continue
            mode = normalize_mode(raw_mode)
            if mode is None:
                warnings.append(
                    'sampling.routes[{!r}] ({}): invalid mode {!r}, using "on"'.format(
                        raw_key, source, raw_mode
                    )
                )
            # Later wins (SAMPLE-013). Re-insert so a later duplicate also
            # takes the later position.
            config.routes.pop(key, None)
            config.routes[key] = "on" if mode is None else mode

    return config, warnings


def resolve_mode(config: SamplingConfig, method: str, route: Optional[str]) -> Mode:
    """SAMPLE-020..022. ``route`` is the matched pattern, None when the
    framework has no router or nothing matched."""
    if route:
        path = normalize_path(route)
        if path is not None:
            exact = config.routes.get("{} {}".format(_ascii_upper(method or ""), path))
            if exact is not None:
                return exact
            wildcard = config.routes.get("* {}".format(path))
            if wildcard is not None:
                return wildcard
    return config.default


def decide(mode: Any, status: int, u: float) -> str:
    """SAMPLE-030, SAMPLE-031. ``u`` is uniform in [0, 1), drawn once per
    request."""
    if mode == "off":
        return DROP
    is_error = status >= 400
    if mode == "errors":
        return FULL if is_error else DROP
    if not _is_number(mode) or is_error:
        return FULL
    return FULL if u < mode else STUB


def canonical_sampling(config: SamplingConfig) -> Optional[str]:
    """SAMPLE-040. The text the hash is computed over, or None for the
    default configuration (SAMPLE-041: nothing to report)."""
    if config.is_default:
        return None
    # Keys are printable ASCII (SAMPLE-010), so code point order is byte order.
    lines = ["default\t{}\n".format(_mode_token(config.default))]
    for key in sorted(config.routes):
        lines.append("{}\t{}\n".format(key, _mode_token(config.routes[key])))
    return "".join(lines)


def sampling_hash(config: SamplingConfig) -> Optional[str]:
    """SAMPLE-040. Lowercase hex SHA-256 of the canonical form, or None."""
    canonical = canonical_sampling(config)
    if canonical is None:
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _mode_token(mode: Mode) -> str:
    """``0.1`` becomes ``"0.1000"``, built from the integer step count and
    never from float formatting."""
    if isinstance(mode, str):
        return mode
    steps = math.floor(mode * _RATE_STEPS + 0.5)
    return "0.{:04d}".format(steps)


def _ascii_upper(s: str) -> str:
    # str.upper() is Unicode-aware: "ß" becomes "SS" here and stays put in JS.
    return s.translate(_ASCII_UPPER)
