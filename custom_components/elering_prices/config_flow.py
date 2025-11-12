from __future__ import annotations
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from .const import DOMAIN, SUPPORTED_COUNTRIES, CONF_COUNTRY, CONF_VAT

class EleringConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            country = user_input[CONF_COUNTRY].lower()
            vat = float(user_input[CONF_VAT])
            if country not in SUPPORTED_COUNTRIES:
                errors["base"] = "invalid_country"
            elif vat < 0 or vat > 100:
                errors["base"] = "invalid_vat"
            else:
                return self.async_create_entry(
                    title=f"Elering ({country.upper()})",
                    data={CONF_COUNTRY: country, CONF_VAT: vat},
                )

        schema = vol.Schema({
            vol.Required(CONF_COUNTRY, default="ee"): vol.In(SUPPORTED_COUNTRIES),
            vol.Required(CONF_VAT, default=24.0): vol.Coerce(float),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
