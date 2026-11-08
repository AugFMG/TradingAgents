"""Bounded official API requests, persistent quotas and freshness-aware cache."""

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta

import requests


class ProviderError(RuntimeError):
    pass


def utc_now():
    return datetime.now(UTC).isoformat()


class Transport:
    def __init__(self, store, provider, ttl=3600, timeout=30, registered=False):
        self.store, self.provider, self.ttl, self.timeout = store, provider, ttl, timeout
        self.registered = registered
        self.cache_hits = 0
        self.requests = 0

    def get(self, url, params=None, body=None):
        # Secrets are neither cache keys in plaintext nor stored URLs/payloads.
        safe_body = {
            k: v for k, v in (body or {}).items() if k not in {"registrationkey", "api_key"}
        }
        safe_params = {k: v for k, v in (params or {}).items() if k not in {"api_key"}}
        key = hashlib.sha256(
            json.dumps([url, safe_params, safe_body], sort_keys=True).encode()
        ).hexdigest()
        cached = self.store.cache_get(key)
        now = datetime.now(UTC)
        if cached and (now - datetime.fromisoformat(cached[0])).total_seconds() < self.ttl:
            self.cache_hits += 1
            return cached[1], cached[0]
        # Conservative per-database budget. IP/account usage by other programs
        # can still exhaust the upstream quota; HTTP 429 is never retried here.
        limit, seconds = {"oecd": (55, 3600), "bls": (450 if self.registered else 20, 86400)}.get(
            self.provider, (240, 3600)
        )
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (provider TEXT, requested_at TEXT)")
            db.execute("BEGIN IMMEDIATE")
            count = db.execute(
                "SELECT COUNT(*) FROM requests WHERE provider=? AND requested_at>?",
                (self.provider, (now - timedelta(seconds=seconds)).isoformat()),
            ).fetchone()[0]
            if count >= limit:
                raise ProviderError("LOCAL_QUOTA_EXHAUSTED: wait for next refresh window")
            db.execute("INSERT INTO requests VALUES (?,?)", (self.provider, now.isoformat()))
        self.requests += 1
        time.sleep(0.2)
        try:
            response = requests.request(
                "POST" if body is not None else "GET",
                url,
                params=params,
                json=body,
                timeout=self.timeout,
                headers={"User-Agent": "TradingAgents macro research/1.0"},
            )
            if response.status_code >= 400:
                raise ProviderError(
                    f"HTTP_{response.status_code}; Retry-After={response.headers.get('Retry-After', 'unknown')}"
                )
            if len(response.content) > 25_000_000:
                raise ProviderError("RESPONSE_TOO_LARGE: narrow countries/window")
            text = response.content.decode("utf-8-sig")
        except requests.RequestException as exc:
            raise ProviderError(f"NETWORK_{type(exc).__name__}") from None
        fetched = utc_now()
        self.store.cache_put(key, fetched, text)
        return text, fetched
