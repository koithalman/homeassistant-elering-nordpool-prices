from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

ELERING_URL_JSON = "https://dashboard.elering.ee/api/nps/price"


def _day_bounds_22utc(now_utc: datetime) -> Tuple[datetime, datetime]:
    """Return (start,end) for the 24h window that Elering uses: 22:00Z → 22:00Z."""
    today_22 = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
    if now_utc < today_22:
        start = today_22 - timedelta(days=1)
        end = today_22
    else:
        start = today_22
        end = today_22 + timedelta(days=1)
    return start, end


class EleringCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """Fetches Elering prices once per 22:00Z window and serves sensors."""

    def __init__(self, hass: HomeAssistant, *, country: str, vat_percent: float) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="elering_prices",
            # We cache the whole 24h window; interval only matters for retries/backoff
            update_interval=timedelta(minutes=10),
        )
        self._country = country.lower()
        self._vat_factor = 1.0 + (vat_percent / 100.0)
        self._cache: Dict[str, Any] = {}
        self._cache_window: Tuple[int, int] | None = None

    # ---- helpers used by sensor.py ----
    def now_ts(self) -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def country(self) -> str:
        return self._country

    # -----------------------------------

    async def _async_update_data(self) -> Dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        start, end = _day_bounds_22utc(now_utc)
        win = (int(start.timestamp()), int(end.timestamp()))

        # Serve cached day if we already fetched this 22:00→22:00 window
        if self._cache and self._cache_window == win:
            return self._cache

        params = {
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
        }

        # Fetch JSON: {"success": true, "data": { "ee": [ { "timestamp": 1726257600, "ee": "60.0000" }, ... ]}}
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(ELERING_URL_JSON, params=params, timeout=20) as resp:
                    if resp.status != 200:
                        raise UpdateFailed(f"Elering HTTP {resp.status}")
                    js = await resp.json()
        except Exception as e:
            raise UpdateFailed(f"Elering fetch failed: {e}") from e

        data = js.get("data")
        if not isinstance(data, dict):
            raise UpdateFailed("Elering JSON missing 'data' dict")

        series = data.get(self._country)
        if not isinstance(series, list):
            raise UpdateFailed(f"Elering JSON has no list for country '{self._country}'")

        # Build hourly list (€/MWh, VAT included)
        hours: List[Dict[str, Any]] = []
        for row in series:
            ts = row.get("timestamp")
            if ts is None:
                continue
            try:
                ts = int(ts)
            except Exception:
                continue

            raw = row.get(self._country)
            if raw is None:
                # Some responses also include "price" instead of country key
                raw = row.get("price")
            if raw is None:
                continue

            try:
                price = float(raw)
            except Exception:
                continue

            hours.append({"ts": ts, "price": round(price * self._vat_factor, 5)})

        hours.sort(key=lambda x: x["ts"])

        # Expand each hour into 4 quarter-hours (same price). This keeps your quarter sensors populated.
        quarters: List[Dict[str, Any]] = []
        for h in hours:
            base = h["ts"]
            p = h["price"]
            quarters.append({"ts": base + 0 * 900, "price": p})
            quarters.append({"ts": base + 1 * 900, "price": p})
            quarters.append({"ts": base + 2 * 900, "price": p})
            quarters.append({"ts": base + 3 * 900, "price": p})

        payload: Dict[str, Any] = {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "country": self._country,
            "start_utc": params["start"],
            "end_utc": params["end"],
            "quarters": quarters,
            "hours": hours,
        }

        self._cache = payload
        self._cache_window = win
        return payload
