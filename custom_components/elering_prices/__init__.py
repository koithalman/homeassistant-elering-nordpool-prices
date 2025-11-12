from __future__ import annotations

from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import UNDEFINED, UndefinedType

from .coordinator import EleringCoordinator

DOMAIN = "elering_prices"
PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Elering Prices from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    country: str = entry.data.get("country", "ee")
    vat: float | UndefinedType = entry.data.get("vat", UNDEFINED)
    vat_percent: float = float(vat) if vat is not UNDEFINED else 24.0  # sensible default

    coord = EleringCoordinator(hass, country=country, vat_percent=vat_percent)
    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coord}

    # First fetch so entities have data immediately
    await coord.async_config_entry_first_refresh()

    # Start the clock-aligned scheduler (00/15/30/45). Change inside coordinator if you want only top of hour.
    coord.start_scheduler()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data: dict[str, Any] = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coord: EleringCoordinator | None = data.get("coordinator")

    if coord:
        coord.stop_scheduler()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
