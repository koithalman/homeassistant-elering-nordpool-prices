from __future__ import annotations
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform
from .coordinator import EleringCoordinator

DOMAIN = "elering_nordpool"
PLATFORMS = [Platform.SENSOR]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    country = (entry.options.get("country") or entry.data.get("country") or "ee").lower()
    vat = float(entry.options.get("vat", entry.data.get("vat", 24)))

    coord = EleringCoordinator(hass, country=country, vat_percent=vat)
    # Start the refresh but DO NOT await it; let entities be created immediately
    hass.async_create_task(coord.async_refresh())

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coord, "country": country, "vat": vat}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # If options change (country/VAT), reload the entry
    entry.async_on_unload(entry.add_update_listener(_reload_on_update))
    return True

async def _reload_on_update(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok