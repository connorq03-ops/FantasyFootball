"""
cache.py - Persistent SQLite TTL cache + rate limiter for the FantasyPros API.

`SQLiteCache` is copied from NCAAProjectCH `app.py` (lines 30-114) and
`cached_call` mirrors `golf_cached_call` in NCAAProjectCH `golf/golf_app.py`
(lines 179-188), with the rate limit configured for FantasyPros:

    api='fantasypros', max_calls=500, window_seconds=86400  # premium: 500/DAY

The window is a TRUE rolling window (individual call timestamps are persisted),
so a burst near a boundary cannot exceed the quota. The default TTL is 24h so a
single daily prefetch serves every downstream board build, and raw pulls are
snapshotted to disk (timestamped) for mid-draft re-runs.
"""

import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from config import load_config


class RateLimitExceeded(RuntimeError):
    """Raised when the FantasyPros daily call budget is exhausted."""


class SQLiteCache:
    """Thread-safe persistent TTL cache backed by SQLite."""

    def __init__(self, db_path='.fantasypros_cache.db', default_ttl=86400, snapshot_dir=None):
        self.db_path = db_path
        self.default_ttl = default_ttl
        self.snapshot_dir = snapshot_dir
        self._local = threading.local()
        self._init_db()

    def _conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            # isolation_level=None -> autocommit, so check_rate_limit can hold an
            # explicit BEGIN IMMEDIATE transaction around its read-modify-write.
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                               isolation_level=None)
        return self._local.conn

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY, data TEXT, ts REAL, ttl REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, api TEXT, ts REAL)''')
        conn.execute('CREATE INDEX IF NOT EXISTS api_calls_api_ts ON api_calls (api, ts)')
        conn.commit()
        conn.close()

    def _key(self, endpoint, params):
        """Cache key includes the filter params so PPR/superflex/dynasty pulls
        never collide with other formats."""
        raw = endpoint + json.dumps(params, sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, endpoint, params, ttl=None):
        k = self._key(endpoint, params)
        ttl = ttl or self.default_ttl
        try:
            row = self._conn().execute(
                'SELECT data, ts FROM cache WHERE key = ?', (k,)).fetchone()
            if row and (time.time() - row[1]) < ttl:
                return json.loads(row[0])
        except Exception:
            pass
        return None

    def set(self, endpoint, params, data):
        k = self._key(endpoint, params)
        try:
            self._conn().execute(
                'INSERT OR REPLACE INTO cache (key, data, ts, ttl) VALUES (?, ?, ?, ?)',
                (k, json.dumps(data, default=str), time.time(), self.default_ttl))
            self._conn().commit()
        except Exception:
            pass

    def check_rate_limit(self, api='fantasypros', max_calls=50, window_seconds=86400):
        """
        Reserve one call against a TRUE rolling window, returning False when the
        budget is exhausted.

        Individual call timestamps are persisted rather than a single
        calls/window_start counter: a fixed window would reset the counter as
        soon as the first call aged out, letting a burst near the boundary
        exceed the real quota.
        """
        now = time.time()
        conn = self._conn()
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute('DELETE FROM api_calls WHERE api = ? AND ts <= ?',
                         (api, now - window_seconds))
            used = conn.execute('SELECT COUNT(*) FROM api_calls WHERE api = ?',
                                (api,)).fetchone()[0]
            allowed = used < max_calls
            if allowed:
                conn.execute('INSERT INTO api_calls (api, ts) VALUES (?, ?)', (api, now))
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
        return allowed

    def stats(self, window_seconds=86400, max_calls=None):
        """
        Return cache statistics and remaining budget per rolling window.

        `max_calls` must be the limit actually enforced by the caller's
        rate-limit config; it falls back to config.yaml only when omitted.
        """
        try:
            now = time.time()
            conn = sqlite3.connect(self.db_path)
            total = conn.execute('SELECT COUNT(*) FROM cache').fetchone()[0]
            valid = conn.execute('SELECT COUNT(*) FROM cache WHERE (? - ts) < ttl',
                                 (now,)).fetchone()[0]
            rates = conn.execute(
                'SELECT api, COUNT(*), MIN(ts) FROM api_calls WHERE ts > ? GROUP BY api',
                (now - window_seconds,)).fetchall()
            conn.close()
            if max_calls is None:
                max_calls = load_config().get('rate_limit', {}).get('max_calls', 500)
            return {
                'total_entries': total, 'valid_entries': valid,
                'rate_limits': {
                    api: {
                        'calls': calls,
                        'remaining': max(0, max_calls - calls),
                        # The oldest retained call is what frees up budget next.
                        'resets_in': max(0, int(window_seconds - (now - oldest))),
                    } for api, calls, oldest in rates
                }
            }
        except Exception:
            return {'total_entries': 0, 'valid_entries': 0, 'rate_limits': {}}

    # ── Raw snapshots (mid-draft re-runs without re-hitting the API) ──────────

    def snapshot(self, name: str, data: Any) -> Optional[str]:
        """Write a timestamped raw copy of an API pull to the snapshot dir."""
        if not self.snapshot_dir:
            return None
        try:
            os.makedirs(self.snapshot_dir, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            safe = name.replace('/', '_').replace('?', '_')
            path = os.path.join(self.snapshot_dir, f"{safe}_{stamp}.json")
            with open(path, 'w') as fh:
                json.dump(data, fh, default=str)
            return path
        except Exception:
            return None


def build_cache(config: Optional[Dict[str, Any]] = None, base_dir: Optional[str] = None) -> SQLiteCache:
    """Construct a SQLiteCache from config.yaml (`cache:` section)."""
    config = config or load_config()
    base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
    cache_cfg = config.get('cache', {})

    def _abs(p):
        return p if os.path.isabs(p) else os.path.join(base_dir, p)

    return SQLiteCache(
        db_path=_abs(cache_cfg.get('db_path', '.fantasypros_cache.db')),
        default_ttl=cache_cfg.get('default_ttl_seconds', 86400),
        snapshot_dir=_abs(cache_cfg.get('snapshot_dir', 'snapshots')),
    )


def cached_call(cache: SQLiteCache, endpoint: str, params: Dict[str, Any],
                fetch_fn: Callable[[], Any], ttl: Optional[float] = None,
                config: Optional[Dict[str, Any]] = None,
                snapshot: bool = True) -> Any:
    """
    Check the cache first, then call the API on a miss.

    Only true cache misses count against the FantasyPros daily call budget.

    Raises:
        RateLimitExceeded: if the daily budget is exhausted.
    """
    config = config or load_config()
    rl = config.get('rate_limit', {})
    cached = cache.get(endpoint, params, ttl=ttl)
    if cached is not None:
        return cached

    api_name = rl.get('api_name', 'fantasypros')
    max_calls = rl.get('max_calls', 50)
    window = rl.get('window_seconds', 86400)
    if not cache.check_rate_limit(api_name, max_calls=max_calls, window_seconds=window):
        remaining = cache.stats(window_seconds=window,
                                max_calls=max_calls)['rate_limits'].get(api_name, {})
        raise RateLimitExceeded(
            f"FantasyPros API budget exhausted ({max_calls} calls per "
            f"{window // 3600}h). Resets in {remaining.get('resets_in', window)}s. "
            f"Run build_board.py off cached data instead of re-prefetching."
        )

    data = fetch_fn()
    cache.set(endpoint, params, data)
    if snapshot:
        cache.snapshot(endpoint, data)
    return data
