from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import aiohttp
import async_timeout

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
import logging

_LOGGER = logging.getLogger(__name__)

ELERING_URL = "https://dashboard.elering.ee/api/nps/price"


def _day_bounds_22utc(now_utc: datetime) -> tuple[datetime, datetime]:
    """Return [start, end) bounds that run from 22:00Z to 22:00Z."""
    today_22 = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
    if now_utc < today_22:
        start = today_22 - timedelta(days=1)
        end = today_22
    else:
        start = today_22
        end = today_22 + timedelta(days=1)
    return start, end


@dataclass
class PricePoint:
    ts: int
    price: float  # €/MWh VAT-included


class EleringCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """Coordinator fetching 15-min Elering prices, VAT-applied, plus hourly averages."""

    def __init__(self, hass: HomeAssistant, country: str, vat_percent: float) -> None:
        # No periodic timer; we run on exact wall clock via scheduler.
        super().__init__(hass, _LOGGER, name="elering_prices", update_interval=None)
        self._country = country.lower().strip()
        self._vat_factor = 1.0 + (vat_percent / 100.0)
        self._cache: Dict[str, Any] = {}
        self._cache_window: Tuple[int, int] | None = None
        self._unsub_timer = None  # scheduler unsub

    # ----------------------
    # Public helpers for sensors
    # ----------------------
    def now_ts(self) -> int:
        """UTC 'now' as epoch seconds."""
        return int(dt_util.utcnow().timestamp())

    # ----------------------
    # Clock-aligned scheduler
    # ----------------------
    def start_scheduler(self) -> None:
        """Refresh exactly at 00/15/30/45 each hour (second 0)."""
        if self._unsub_timer:
            return

        # If you only care about hourly rollovers, set minute=[0].
        self._unsub_timer = async_track_time_change(
            self.hass,
            self._on_tick,
            second=0,
            minute=[0, 15, 30, 45],
        )
        _LOGGER.debug("Elering scheduler started (aligned to 00/15/30/45).")

    def stop_scheduler(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
            _LOGGER.debug("Elering scheduler stopped.")

    @callback
    async def _on_tick(self, _now) -> None:
        """Kick the coordinator right on time."""
        _LOGGER.debug("Elering scheduler tick → async_request_refresh()")
        await self.async_request_refresh()

    # ----------------------
    # Core fetch
    # ----------------------
    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch (or reuse cached) 22:00Z→22:00Z window and compute outputs."""
        now_utc = dt_util.utcnow()
        start, end = _day_bounds_22utc(now_utc)
        win = (int(start.timestamp()), int(end.timestamp()))

        # Use cached day if already fetched and still valid
        if self._cache and self._cache_window == win:
            return self._cache

        params = {
            "start": start.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
            "end": end.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        }

        session = async_get_clientsession(self.hass)
        try:
            async with async_timeout.timeout(30):
                async with session.get(ELERING_URL, params=params) as resp:
                    if resp.status != 200:
                        raise UpdateFailed(f"Elering HTTP {resp.status}")
                    # Sometimes content-type is odd; don't force it.
                    data = await resp.json(content_type=None)
        except Exception as e:
            raise UpdateFailed(f"Elering fetch failed: {e}") from e

        # API can be: {"data": [...]} or directly [...]
        if isinstance(data, dict):
            rows = data.get("data")
        else:
            rows = data

        if not isinstance(rows, list):
            raise UpdateFailed("Elering JSON missing 'data' list")

        quarters: list[PricePoint] = []

        for row in rows:
            # Timestamp
            ts = row.get("timestamp") or row.get("ts")
            if ts is None:
                continue
            if isinstance(ts, str):
                if ts.isdigit():
                    ts = int(ts)
                else:
                    try:
                        ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        continue
            elif not isinstance(ts, int):
                try:
                    ts = int(ts)
                except Exception:
                    continue

            # Price — prefer country key, fallback to generic "price"
            raw = row.get(self._country)
            if raw is None:
                raw = row.get("price")
            if raw is None:
                continue

            try:
                price = float(raw)  # €/MWh ex-VAT from API
            except Exception:
                continue

            price_vat = round(price * self._vat_factor, 5)  # €/MWh VAT-in
            quarters.append(PricePoint(ts=ts, price=price_vat))

        quarters.sort(key=lambda p: p.ts)

        # Build hourly averages from quarter points (group by hour)
        hourly_buckets: dict[int, list[float]] = defaultdict(list)
        for q in quarters:
            hour_ts = (q.ts // 3600) * 3600
            hourly_buckets[hour_ts].append(q.price)

        hours: list[dict[str, Any]] = []
        for hts in sorted(hourly_buckets.keys()):
            bucket = hourly_buckets[hts]
            hours.append({"ts": hts, "price": sum(bucket) / len(bucket)})

        payload: Dict[str, Any] = {
            "as_of": dt_util.utcnow().isoformat(),
            "country": self._country,
            "vat_percent": round((self._vat_factor - 1.0) * 100.0, 3),
            "start_utc": params["start"],
            "end_utc": params["end"],
            "quarters": [{"ts": p.ts, "price": p.price} for p in quarters],
            "hours": hours,
        }

        self._cache = payload
        self._cache_window = win
        return payload
