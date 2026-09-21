"""Config flow for ElCoTra integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_AUTO_IMPORT_ON_SETUP,
    CONF_CSV_URL_L1,
    CONF_CSV_URL_L2,
    CONF_CSV_URL_L3,
    CONF_CURRENCY,
    CONF_ENERGY_SENSORS,
    CONF_FIXED_TARIFF,
    CONF_NAME,
    CONF_TARIFF_FACTOR,
    CONF_TARIFF_SENSOR,
    CONF_TARIFF_TYPE,
    CURRENCIES,
    DEFAULT_CURRENCY,
    DEFAULT_FIXED_TARIFF,
    DEFAULT_TARIFF_FACTOR,
    DEFAULT_TARIFF_TYPE,
    TARIFF_TYPE_FIXED,
    TARIFF_TYPE_SENSOR,
)


class ElCoTraConfigFlow(config_entries.ConfigFlow, domain="elcotra"):
    """Handle a config flow for ElCoTra."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict = {}

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            if (
                user_input[CONF_TARIFF_TYPE] == TARIFF_TYPE_SENSOR
                and CONF_TARIFF_SENSOR not in user_input
            ):
                errors["tariff_sensor"] = "tariff_sensor_required"
            if not errors:
                self._data.update(user_input)
                return await self.async_step_csv_import()

        data_schema = vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_ENERGY_SENSORS): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", multiple=True),
                ),
                vol.Required(
                    CONF_TARIFF_TYPE, default=DEFAULT_TARIFF_TYPE
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=TARIFF_TYPE_FIXED, label="Fixed price"
                            ),
                            selector.SelectOptionDict(
                                value=TARIFF_TYPE_SENSOR,
                                label="Time-based price from sensor",
                            ),
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_TARIFF_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    CONF_TARIFF_FACTOR, default=DEFAULT_TARIFF_FACTOR
                ): cv.positive_float,
                vol.Optional(
                    CONF_FIXED_TARIFF, default=DEFAULT_FIXED_TARIFF
                ): cv.positive_float,
                vol.Required(
                    CONF_CURRENCY, default=DEFAULT_CURRENCY
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=CURRENCIES,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_csv_import(self, user_input=None):
        """Configure CSV import for historical data."""
        if user_input is not None:
            # Merge CSV configuration with existing data
            self._data.update(user_input)
            return self.async_create_entry(
                title=self._data[CONF_NAME],
                data=self._data,
            )

        data_schema = vol.Schema(
            {
                vol.Optional(CONF_AUTO_IMPORT_ON_SETUP, default=False): cv.boolean,
                vol.Optional(CONF_CSV_URL_L1): str,
                vol.Optional(CONF_CSV_URL_L2): str,
                vol.Optional(CONF_CSV_URL_L3): str,
            }
        )

        return self.async_show_form(
            step_id="csv_import",
            data_schema=data_schema,
            description_placeholders={
                "info": "Optionally configure automatic import of historical energy data from CSV files. Leave blank to skip."
            },
        )
