from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple
import logging
import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_track_time_change

_LOGGER = logging.getLogger(__name__)
ELERING_URL = "https://dashboard.elering.ee/api/nps/price"


def _day_bounds_22utc(now_utc: datetime) -> Tuple[datetime, datetime]:
    """Return [start, end) bounds from 22:00Z to 22:00Z."""
    today_22 = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
    if now_utc < today_22:
        start = today_22 - timedelta(days=1)
        end = today_22
    else:
        start = today_22
        end = today_22 + timedelta(days=1)
    return start, end


class EleringCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    """Fetches Elering quarter-hour prices and hourly means, aligned to wall-clock."""

    def __init__(self, hass: HomeAssistant, country: str, vat_percent: float) -> None:
        # No drifting interval — we'll trigger on exact :00/:15/:30/:45 via scheduler.
        super().__init__(hass, _LOGGER, name="elering_nordpool", update_interval=None)
        self._country = country.lower()
        self._vat_factor = 1.0 + (vat_percent / 100.0)
        self._cache: Dict[str, Any] = {}
        self._cache_window: Tuple[int, int] | None = None
        self._unsub_timer = None  # scheduler unsubscribe handle

    # ---- helpers used by sensors ----
    def now_ts(self) -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def quarters(self) -> List[Dict[str, Any]]:
        return self.data.get("quarters", []) if self.data else []

    def hours(self) -> List[Dict[str, Any]]:
        return self.data.get("hours", []) if self.data else []
    # ---------------------------------

    # ----------------------
    # Wall-clock scheduler
    # ----------------------
    def start_scheduler(self) -> None:
        """Refresh at exact 00/15/30/45 each hour (local time), at second 0."""
        if self._unsub_timer:
            return
        self._unsub_timer = async_track_time_change(
            self.hass,
            self._on_tick,
            second=0,
            minute=[0, 15, 30, 45],
        )
        _LOGGER.debug("Elering scheduler started (aligned to :00/:15/:30/:45).")

    def stop_scheduler(self) -> None:
        """Stop the wall-clock scheduler."""
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
            _LOGGER.debug("Elering scheduler stopped.")

    @callback
    async def _on_tick(self, _now) -> None:
        """Kick the coordinator exactly on schedule."""
        _LOGGER.debug("Quarter-hour tick → async_request_refresh()")
        await self.async_request_refresh()

    # ----------------------
    # Core fetch
    # ----------------------
    async def _async_update_data(self) -> Dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        start, end = _day_bounds_22utc(now_utc)
        win = (int(start.timestamp()), int(end.timestamp()))

        # Use cached day if already fetched for this 22Z-22Z window
        if self._cache and self._cache_window == win:
            return self._cache

        params = {
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "fields": self._country,  # request the country column explicitly
        }

        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                ELERING_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise UpdateFailed(f"Elering HTTP {resp.status}: {text[:200]}")
                try:
                    payload = await resp.json(content_type=None)
                except Exception as je:
                    _LOGGER.error("Elering non-JSON response preview: %s", text[:300])
                    raise UpdateFailed(f"Elering JSON parse failed: {je}") from je
        except Exception as e:
            raise UpdateFailed(f"Elering fetch failed: {e}") from e

        rows = None

        # Variant A: {"data": [ {...}, ... ]}
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            rows = payload["data"]

        # Variant B: top-level list: [ {...}, ... ]
        if rows is None and isinstance(payload, list):
            rows = payload

        # Variant C: {"data": {"series"/"records"/"rows"/<country>: [ {...}, ... ]}}
        if rows is None and isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            d = payload["data"]
            for key in ("series", "records", "rows", self._country):
                if isinstance(d.get(key), list):
                    rows = d[key]
                    break

        if rows is None:
            prev = str(payload)
            _LOGGER.error("Unexpected Elering payload (preview): %s", prev[:400])
            raise UpdateFailed("Elering JSON missing price rows")

        quarters: List[Dict[str, Any]] = []
        for row in rows:
            # Timestamp normalization
            ts_raw = row.get("timestamp") if isinstance(row, dict) else None
            if ts_raw is None and isinstance(row, dict):
                ts_raw = row.get("ts")  # some shapes use "ts"

            ts: int | None = None
            if isinstance(ts_raw, (int, float)):
                ts = int(ts_raw)
            elif isinstance(ts_raw, str):
                if ts_raw.isdigit():
                    ts = int(ts_raw)
                else:
                    try:
                        ts = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        ts = None
            if ts is None:
                continue

            # Price in country column or generic "price"
            price_raw = None
            if isinstance(row, dict):
                price_raw = row.get(self._country)
                if price_raw is None:
                    price_raw = row.get("price")
            if price_raw is None:
                continue

            try:
                price = float(price_raw)
            except Exception:
                continue

            price_vat = round(price * self._vat_factor, 5)
            quarters.append({"ts": ts, "price": price_vat})

        quarters.sort(key=lambda x: x["ts"])

        # Build hourly averages (mean of available quarters in that hour)
        hours: List[Dict[str, Any]] = []
        if quarters:
            cur_hour = None
            bucket: List[float] = []
            for q in quarters:
                hts = (q["ts"] // 3600) * 3600
                if cur_hour is None:
                    cur_hour = hts
                if hts != cur_hour:
                    if bucket:
                        hours.append({"ts": cur_hour, "price": sum(bucket) / len(bucket)})
                    cur_hour = hts
                    bucket = []
                bucket.append(q["price"])
            if bucket:
                hours.append({"ts": cur_hour, "price": sum(bucket) / len(bucket)})

        result: Dict[str, Any] = {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "country": self._country,
            "start_utc": params["start"],
            "end_utc": params["end"],
            "quarters": quarters,
            "hours": hours,
        }

        self._cache = result
        self._cache_window = win

        _LOGGER.debug(
            "Fetched %d quarters, %d hours for %s (VAT factor %.3f)",
            len(quarters), len(hours), self._country, self._vat_factor
        )
        return result
