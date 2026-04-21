#!/usr/bin/env python3
"""Utilities for fetching Finland spot electricity prices with caching."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

import requests

SPOT_PRICE_CACHE_TTL_SECONDS = 300
SPOT_SERIES_CACHE_TTL_SECONDS = 300
SPOT_HINTA_URL = "https://api.spot-hinta.fi/JustNow"
PORSSISAHKO_URL = "https://api.porssisahko.net/v1/latest-prices.json"

# Finnish VAT on electricity (25.5% from Sept 2024). Applied to porssisahko raw
# prices so chart series is consistent with the spot-hinta.fi "PriceWithTax"
# value shown on the dashboard badge.
FI_ELECTRICITY_VAT_RATE = 0.255

_series_cache_lock = threading.Lock()
_series_fetched_at = 0.0

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {
    "fetched_at": 0.0,
    "data": None,
}


def _copy_price_data(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if data is None:
        return None
    return dict(data)


def _normalize_price(
    *,
    price_eur_per_kwh: float,
    effective_at: Optional[str],
    source: str,
    vat_included: bool,
    stale: bool = False,
) -> Dict[str, Any]:
    price_eur_per_kwh = float(price_eur_per_kwh)
    return {
        "available": True,
        "country": "FI",
        "price_eur_per_kwh": price_eur_per_kwh,
        "price_cents_per_kwh": price_eur_per_kwh * 100.0,
        "effective_at": effective_at,
        "source": source,
        "vat_included": vat_included,
        "stale": stale,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }



def _fetch_from_spot_hinta() -> Dict[str, Any]:
    response = requests.get(SPOT_HINTA_URL, timeout=10)
    response.raise_for_status()
    payload = response.json()

    if "PriceWithTax" not in payload:
        raise ValueError("PriceWithTax missing from spot-hinta response")

    return _normalize_price(
        price_eur_per_kwh=float(payload["PriceWithTax"]),
        effective_at=payload.get("DateTime"),
        source="spot-hinta.fi",
        vat_included=True,
    )



def _fetch_from_porssisahko() -> Dict[str, Any]:
    response = requests.get(PORSSISAHKO_URL, timeout=10)
    response.raise_for_status()
    payload = response.json()
    prices = payload.get("prices") or []
    if not prices:
        raise ValueError("No prices in porssisahko response")

    now = datetime.now(timezone.utc)
    current = None
    for item in prices:
        start = datetime.fromisoformat(item["startDate"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(item["endDate"].replace("Z", "+00:00"))
        if start <= now < end:
            current = item
            break

    if current is None:
        current = prices[0]

    # api.porssisahko.net returns cents/kWh
    price_cents_per_kwh = float(current["price"])
    return _normalize_price(
        price_eur_per_kwh=price_cents_per_kwh / 100.0,
        effective_at=current.get("startDate"),
        source="api.porssisahko.net",
        vat_included=False,
    )



def get_fi_spot_price(force_refresh: bool = False) -> Dict[str, Any]:
    """Return Finland spot price with small in-memory cache.

    The function prefers a VAT-inclusive source for direct UI cost calculations.
    If both upstream APIs fail, a stale cached value is returned when available.
    """
    now = time.time()

    with _cache_lock:
        cached = _copy_price_data(_cache["data"])
        fetched_at = float(_cache["fetched_at"] or 0.0)

    if not force_refresh and cached and (now - fetched_at) < SPOT_PRICE_CACHE_TTL_SECONDS:
        return cached

    errors = []
    for fetcher in (_fetch_from_spot_hinta, _fetch_from_porssisahko):
        try:
            fresh = fetcher()
            with _cache_lock:
                _cache["data"] = fresh
                _cache["fetched_at"] = now
            return _copy_price_data(fresh) or {"available": False}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fetcher.__name__}: {exc}")

    if cached:
        cached["stale"] = True
        cached["error"] = "; ".join(errors)
        return cached

    return {
        "available": False,
        "country": "FI",
        "source": None,
        "error": "; ".join(errors) if errors else "Unknown spot price error",
        "stale": True,
    }



def get_price_eur_per_kwh(default_eur_per_kwh: float = 0.25) -> float:
    """Resolve current usable electricity price for calculations."""
    spot = get_fi_spot_price()
    if spot.get("available") and spot.get("price_eur_per_kwh") is not None:
        return float(spot["price_eur_per_kwh"])
    return float(default_eur_per_kwh)


def ensure_spot_price_table(db_path: str) -> None:
    """Create the spot_prices table if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS spot_prices (
                start_ts REAL PRIMARY KEY,
                end_ts REAL NOT NULL,
                price_eur_per_kwh REAL NOT NULL,
                source TEXT,
                vat_included INTEGER
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_porssisahko_series() -> List[Dict[str, Any]]:
    response = requests.get(PORSSISAHKO_URL, timeout=10)
    response.raise_for_status()
    payload = response.json()
    prices = payload.get("prices") or []
    entries: List[Dict[str, Any]] = []
    for item in prices:
        start = datetime.fromisoformat(item["startDate"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(item["endDate"].replace("Z", "+00:00"))
        raw_cents = float(item["price"])  # ex-VAT, c/kWh
        price_eur_per_kwh = (raw_cents / 100.0) * (1.0 + FI_ELECTRICITY_VAT_RATE)
        entries.append(
            {
                "start_ts": start.timestamp(),
                "end_ts": end.timestamp(),
                "price_eur_per_kwh": price_eur_per_kwh,
                "source": "api.porssisahko.net",
                "vat_included": True,
            }
        )
    return entries


def _refresh_series_cache(db_path: str) -> None:
    """Fetch latest porssisahko series and upsert into SQLite. Rate-limited."""
    global _series_fetched_at
    now = time.time()
    with _series_cache_lock:
        if now - _series_fetched_at < SPOT_SERIES_CACHE_TTL_SECONDS:
            return
        try:
            entries = _fetch_porssisahko_series()
        except Exception:
            # Brief backoff on failure, don't hammer upstream
            _series_fetched_at = now - SPOT_SERIES_CACHE_TTL_SECONDS + 30
            return
        ensure_spot_price_table(db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.executemany(
                """
                INSERT OR REPLACE INTO spot_prices
                (start_ts, end_ts, price_eur_per_kwh, source, vat_included)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        e["start_ts"],
                        e["end_ts"],
                        e["price_eur_per_kwh"],
                        e["source"],
                        int(e["vat_included"]),
                    )
                    for e in entries
                ],
            )
            conn.commit()
        finally:
            conn.close()
        _series_fetched_at = now


def get_fi_spot_price_series(
    start_ts: float,
    end_ts: float,
    db_path: str = "p1_data.db",
) -> List[Dict[str, Any]]:
    """Return cached spot price entries overlapping [start_ts, end_ts].

    Triggers an upstream refresh if the cache is stale. Historical coverage is
    limited to whatever porssisahko.net has returned during prior calls, since
    that source only publishes a ~48h rolling window.
    """
    ensure_spot_price_table(db_path)
    _refresh_series_cache(db_path)

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            """
            SELECT start_ts, end_ts, price_eur_per_kwh, source, vat_included
            FROM spot_prices
            WHERE end_ts >= ? AND start_ts <= ?
            ORDER BY start_ts ASC
            """,
            (start_ts, end_ts),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [
        {
            "start_ts": row[0],
            "end_ts": row[1],
            "start": datetime.fromtimestamp(row[0], tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(row[1], tz=timezone.utc).isoformat(),
            "price_eur_per_kwh": row[2],
            "price_cents_per_kwh": row[2] * 100.0,
            "source": row[3],
            "vat_included": bool(row[4]),
        }
        for row in rows
    ]
