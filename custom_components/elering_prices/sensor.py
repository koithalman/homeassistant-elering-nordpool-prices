from __future__ import annotations
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.const import UnitOfEnergy
from . import DOMAIN
from .coordinator import EleringCoordinator

ENTITY_PREFIX = "Elering"

S_QUARTER_MWH = "elering_quarter_price_mwh"
S_QUARTER_S_KWH = "elering_quarter_price_s_per_kwh"
S_HOURLY_MWH = "elering_hourly_avg_mwh"
S_HOURLY_S_KWH = "elering_hourly_avg_s_per_kwh"

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    data = hass.data[DOMAIN][entry.entry_id]
    coord: EleringCoordinator = data["coordinator"]
    country = data["country"].upper()

    ents: list[SensorEntity] = [
        QuarterPriceMWh(coord, f"{ENTITY_PREFIX} Quarter Price ({country})", S_QUARTER_MWH),
        QuarterPriceSkWh(coord, f"{ENTITY_PREFIX} Quarter Price s/kWh", S_QUARTER_S_KWH),
        HourlyAvgMWh(coord, f"{ENTITY_PREFIX} Hourly Avg (€/MWh)", S_HOURLY_MWH),
        HourlyAvgSkWh(coord, f"{ENTITY_PREFIX} Hourly Avg (s/kWh)", S_HOURLY_S_KWH),
    ]
    async_add_entities(ents)  # add now; states become available after first refresh

class _Base(CoordinatorEntity[EleringCoordinator], SensorEntity):
    _attr_state_class = "measurement"

    def __init__(self, coordinator: EleringCoordinator, name: str, unique_id: str):
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_unique_id = unique_id

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        # expose full day windows for automations
        return {
            "as_of": data.get("as_of"),
            "country": data.get("country"),
            "start_utc": data.get("start_utc"),
            "end_utc": data.get("end_utc"),
            "quarters": data.get("quarters"),
            "hours": data.get("hours"),
        }

class QuarterPriceMWh(_Base):
    _attr_native_unit_of_measurement = "€/MWh"

    @property
    def native_value(self):
        d = self.coordinator.data or {}
        now_ts = self.coordinator.now_ts()
        q = None
        for item in d.get("quarters", []) or []:
            if item["ts"] <= now_ts:
                q = item
            else:
                break
        return round(q["price"], 2) if q else None

class QuarterPriceSkWh(_Base):
    _attr_native_unit_of_measurement = "s/kWh"

    @property
    def native_value(self):
        d = self.coordinator.data or {}
        now_ts = self.coordinator.now_ts()
        q = None
        for item in d.get("quarters", []) or []:
            if item["ts"] <= now_ts:
                q = item
            else:
                break
        # 1 €/MWh = 0.1 s/kWh
        return round((q["price"] / 10.0), 2) if q else None

class HourlyAvgMWh(_Base):
    _attr_native_unit_of_measurement = "€/MWh"

    @property
    def native_value(self):
        d = self.coordinator.data or {}
        now_hour = (self.coordinator.now_ts() // 3600) * 3600
        for h in d.get("hours", []) or []:
            if h["ts"] == now_hour:
                return round(h["price"], 2)
        return None

class HourlyAvgSkWh(_Base):
    _attr_native_unit_of_measurement = "s/kWh"

    @property
    def native_value(self):
        d = self.coordinator.data or {}
        now_hour = (self.coordinator.now_ts() // 3600) * 3600
        for h in d.get("hours", []) or []:
            if h["ts"] == now_hour:
                return round(h["price"] / 10.0, 2)
        return None
