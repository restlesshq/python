"""In-process caches.

Implements CONTRACT.md section 11.
"""

import time
from typing import Any, Dict, Optional

# CACHE-004, CACHE-014
_ENRICH_TTL_MS = 60 * 60 * 1000
_RECOVERY_TTL_MS = 60 * 60 * 1000
_RECOVERY_NEGATIVE_TTL_MS = 5 * 60 * 1000


def _now_ms() -> float:
    return time.monotonic() * 1000.0


class EnrichCache:
    """CACHE-001..007.

    Stores the enriched VALUE, not merely a freshness flag (CACHE-003). Every
    upload has to carry owner metadata, including uploads that skipped the
    callback, because the ingest cannot backfill it: without the value, every
    request after the first would land in the dashboard as unauthenticated.
    """

    def __init__(self, ttl_ms: float = _ENRICH_TTL_MS):
        self._cache: Dict[str, Any] = {}
        self._ttl_ms = ttl_ms

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        value, ts = entry
        if _now_ms() - ts > self._ttl_ms:
            self._cache.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Dict[str, Any]) -> None:
        self._cache[key] = (value, _now_ms())

    def invalidate(self, key: str) -> None:
        """CACHE-006. Next request from this key re-runs enrich."""
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()

    def size(self) -> int:
        return len(self._cache)


class RecoveryCache:
    """CACHE-010..015. Fingerprint key to recovery message.

    Every read is synchronous and in-process. The lookup sits on the hot path
    of every 4xx/5xx, so it never performs I/O and never blocks the response;
    a cold miss simply injects nothing.

    "No message for this fingerprint" is itself cached (as ``None``) so a cold
    miss does not stay a cold miss on every subsequent request. The negative
    TTL is shorter so a freshly-attached dashboard message starts working
    within minutes.
    """

    def __init__(
        self,
        ttl_ms: float = _RECOVERY_TTL_MS,
        negative_ttl_ms: float = _RECOVERY_NEGATIVE_TTL_MS,
    ):
        self._cache: Dict[str, Any] = {}
        self._ttl_ms = ttl_ms
        self._negative_ttl_ms = negative_ttl_ms

    _MISS = object()

    def get(self, key: str) -> Any:
        """Returns the message, ``None`` for a confirmed absence, or ``_MISS``.

        The three-way result matters: ``None`` means the server told us there
        is no message (do not ask again yet), while ``_MISS`` means we have
        never asked.
        """
        entry = self._cache.get(key)
        if entry is None:
            return self._MISS
        message, ts = entry
        ttl = self._negative_ttl_ms if message is None else self._ttl_ms
        if _now_ms() - ts > ttl:
            self._cache.pop(key, None)
            return self._MISS
        return message

    def set(self, key: str, message: Optional[str]) -> None:
        self._cache[key] = (message, _now_ms())

    def lookup(self, key: str) -> Optional[str]:
        """CACHE-010. The hot-path read: a message to inject, or None."""
        value = self.get(key)
        return value if isinstance(value, str) else None

    def invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()

    def size(self) -> int:
        return len(self._cache)


class Blocklist:
    """Masked keys to reject. O(1) lookup, per-process state."""

    def __init__(self) -> None:
        self._blocked = set()

    def replace(self, masked_keys) -> None:
        self._blocked = set(masked_keys)

    def has(self, masked_key: Optional[str]) -> bool:
        return bool(masked_key) and masked_key in self._blocked

    def size(self) -> int:
        return len(self._blocked)
