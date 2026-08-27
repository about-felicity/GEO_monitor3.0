"""Bounded JSON response micro-cache with optional Redis sharing.

The analytics layer already caches query results.  This cache sits one layer
closer to HTTP so a large crowd requesting the same filter also reuses JSON
serialization and gzip work.  Stale-while-revalidate keeps a cold aggregation
from occupying every request worker at once.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
import gzip
import hashlib
import json
import os
import struct
import threading
import time
from typing import Any, Callable
from weakref import WeakValueDictionary

try:
    import redis
except ImportError:  # Redis is optional; PostgreSQL/disk remain the fallback.
    redis = None


@dataclass(frozen=True)
class CachedJSONResponse:
    body: bytes
    gzip_body: bytes
    etag: str
    created_at: float
    fresh_until: float
    stale_until: float


class JSONResponseCache:
    def __init__(self) -> None:
        self.max_entries = max(16, min(2048, int(os.getenv("MONITOR_HTTP_CACHE_MAX", "256"))))
        self.redis_max_bytes = max(
            64 * 1024,
            int(os.getenv("MONITOR_HTTP_REDIS_MAX_BYTES", str(8 * 1024 * 1024))),
        )
        self._entries: OrderedDict[str, CachedJSONResponse] = OrderedDict()
        self._locks: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()
        self._building: set[str] = set()
        self._lock = threading.RLock()
        self._redis = None
        self._redis_retry_at = 0.0
        self._counters = defaultdict(int)

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @staticmethod
    def _redis_key(key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"monitor:http:v1:{digest}"

    @staticmethod
    def _make_entry(body: bytes, created_at: float, ttl: float, stale_ttl: float) -> CachedJSONResponse:
        digest = hashlib.sha256(body).hexdigest()
        return CachedJSONResponse(
            body=body,
            gzip_body=gzip.compress(body, compresslevel=1),
            etag=f'"{digest}"',
            created_at=created_at,
            fresh_until=created_at + max(0.1, ttl),
            stale_until=created_at + max(ttl, stale_ttl),
        )

    def _remember(self, key: str, entry: CachedJSONResponse) -> None:
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = entry
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def _local(self, key: str, now: float) -> CachedJSONResponse | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.stale_until <= now:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry

    def _redis_client(self):
        url = os.getenv("MONITOR_REDIS_URL", "").strip()
        if not url or redis is None or time.monotonic() < self._redis_retry_at:
            return None
        if self._redis is None:
            try:
                client = redis.Redis.from_url(
                    url,
                    socket_connect_timeout=0.25,
                    socket_timeout=0.5,
                    health_check_interval=30,
                    decode_responses=False,
                )
                client.ping()
                self._redis = client
            except Exception:
                self._redis = None
                self._redis_retry_at = time.monotonic() + 30.0
        return self._redis

    def _redis_get(self, key: str, ttl: float, stale_ttl: float) -> CachedJSONResponse | None:
        client = self._redis_client()
        if client is None:
            return None
        try:
            packed = client.get(self._redis_key(key))
            if not packed or len(packed) < 9:
                return None
            created_at = struct.unpack("!d", packed[:8])[0]
            entry = self._make_entry(packed[8:], created_at, ttl, stale_ttl)
            if entry.stale_until <= time.time():
                return None
            self._counters["redis_hits"] += 1
            self._remember(key, entry)
            return entry
        except Exception:
            self._redis = None
            self._redis_retry_at = time.monotonic() + 30.0
            return None

    def _redis_put(self, key: str, entry: CachedJSONResponse, stale_ttl: float) -> None:
        if len(entry.body) > self.redis_max_bytes:
            return
        client = self._redis_client()
        if client is None:
            return
        try:
            packed = struct.pack("!d", entry.created_at) + entry.body
            client.set(self._redis_key(key), packed, ex=max(1, int(stale_ttl)))
            self._counters["redis_writes"] += 1
        except Exception:
            self._redis = None
            self._redis_retry_at = time.monotonic() + 30.0

    def _build(
        self,
        key: str,
        builder: Callable[[], Any],
        ttl: float,
        stale_ttl: float,
    ) -> CachedJSONResponse:
        payload = builder()
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        entry = self._make_entry(body, time.time(), ttl, stale_ttl)
        self._remember(key, entry)
        self._redis_put(key, entry, stale_ttl)
        self._counters["builds"] += 1
        return entry

    def _refresh_in_background(
        self,
        key: str,
        builder: Callable[[], Any],
        ttl: float,
        stale_ttl: float,
    ) -> None:
        with self._lock:
            if key in self._building:
                return
            self._building.add(key)

        def refresh() -> None:
            try:
                with self._key_lock(key):
                    self._build(key, builder, ttl, stale_ttl)
            except Exception:
                self._counters["refresh_errors"] += 1
            finally:
                with self._lock:
                    self._building.discard(key)

        threading.Thread(
            target=refresh,
            name="http-json-cache-refresh",
            daemon=True,
        ).start()

    def get_or_build(
        self,
        key: str,
        builder: Callable[[], Any],
        *,
        ttl: float = 3.0,
        stale_ttl: float = 30.0,
    ) -> tuple[CachedJSONResponse, str]:
        now = time.time()
        entry = self._local(key, now)
        if entry is not None:
            if entry.fresh_until > now:
                self._counters["hits"] += 1
                return entry, "HIT"
            self._counters["stale_hits"] += 1
            self._refresh_in_background(key, builder, ttl, stale_ttl)
            return entry, "STALE"

        entry = self._redis_get(key, ttl, stale_ttl)
        if entry is not None:
            if entry.fresh_until <= now:
                self._refresh_in_background(key, builder, ttl, stale_ttl)
                return entry, "REDIS-STALE"
            return entry, "REDIS"

        with self._key_lock(key):
            entry = self._local(key, time.time())
            if entry is not None:
                self._counters["coalesced"] += 1
                return entry, "COALESCED"
            self._counters["misses"] += 1
            return self._build(key, builder, ttl, stale_ttl), "MISS"

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "building": len(self._building),
                "max_entries": self.max_entries,
                "redis_configured": bool(os.getenv("MONITOR_REDIS_URL", "").strip()),
                "redis_connected": self._redis is not None,
                **dict(self._counters),
            }


HTTP_JSON_CACHE = JSONResponseCache()
