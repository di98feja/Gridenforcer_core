"""Coordinator fetching sensor data."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_SENSORS,
    CONF_FIXED_TARIFF,
    CONF_TARIFF_FACTOR,
    CONF_TARIFF_SENSOR,
    CONF_TARIFF_TYPE,
    DEFAULT_TARIFF_FACTOR,
    TARIFF_TYPE_SENSOR,
)

_LOGGER = logging.getLogger(__name__)


class ElCoTraCoordinator(DataUpdateCoordinator):
    """Class to manage fetching energy and tariff data."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        self.entry = entry
        self.energy_sensors: list[str] = entry.data[CONF_ENERGY_SENSORS]
        self.tariff_type: str = entry.data[CONF_TARIFF_TYPE]
        self.tariff_sensor: str | None = entry.data.get(CONF_TARIFF_SENSOR)
        self.fixed_tariff: float = entry.data.get(CONF_FIXED_TARIFF, 0.0)
        self.tariff_factor: float = entry.data.get(
            CONF_TARIFF_FACTOR, DEFAULT_TARIFF_FACTOR
        )

        self._last_energy: float | None = None
        self._last_update: datetime | None = None
        self._accumulated_cost: float = 0.0
        self._unsub_time_tracker = None
        self._unsub_state_tracker = None

        super().__init__(
            hass,
            _LOGGER,
            name="ElCoTra Coordinator",
            update_interval=None,
        )

    async def async_start(self):
        """Start the coordinator."""

        # Vänta på att alla energisensorer blir tillgängliga
        await self._wait_for_sensors()

        # Run initial update to populate data
        await self.async_refresh()

        self._unsub_time_tracker = async_track_time_change(
            self.hass, self._scheduled_update, minute=[0, 15, 30, 45], second=0
        )
        _LOGGER.info("ElCoTra scheduled to run every quarter hours")

        # Source sensors (e.g. the price sensor from another integration) may
        # come up after us. Retry as soon as they do instead of waiting for
        # the next quarter hour.
        sources = [*self.energy_sensors]
        if self.tariff_type == TARIFF_TYPE_SENSOR and self.tariff_sensor:
            sources.append(self.tariff_sensor)
        self._unsub_state_tracker = async_track_state_change_event(
            self.hass, sources, self._source_state_changed
        )

    @callback
    def _source_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Refresh right away when a source sensor recovers after a failure."""
        if self.last_update_success:
            return
        new_state = event.data["new_state"]
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        _LOGGER.info("%s is available again, refreshing", new_state.entity_id)
        self.hass.async_create_task(self.async_refresh())

    async def _wait_for_sensors(self, timeout: int = 5):
        """Wait for energy sensors to become available."""
        start = dt_util.utcnow()

        while (dt_util.utcnow() - start).total_seconds() < timeout:
            all_available = True
            for sensor_id in self.energy_sensors:
                state = self.hass.states.get(sensor_id)
                if not state or state.state in ("unavailable", "unknown"):
                    all_available = False
                    break

            if all_available:
                _LOGGER.info("All energy sensors are available")
                return

            await asyncio.sleep(1)
        _LOGGER.warning("Timeout waiting for all energy sensors - starting anyway")

    async def async_stop(self):
        """Stop the coordinator."""
        if self._unsub_time_tracker:
            self._unsub_time_tracker()
            self._unsub_time_tracker = None
        if self._unsub_state_tracker:
            self._unsub_state_tracker()
            self._unsub_state_tracker = None

    def restore_state(self, last_energy: float | None, accumulated_cost: float) -> None:
        """Restore state from previous run."""
        self._last_energy = last_energy
        self._accumulated_cost = accumulated_cost
        _LOGGER.info(
            "Restored coordinator state: last_energy=%.4f, accumulated_cost=%.4f",
            last_energy or 0,
            accumulated_cost,
        )

    @callback
    def _scheduled_update(self, now):
        """Callback for scheduled updates."""
        _LOGGER.debug("Scheduled update triggered at %s", now)
        self.hass.async_create_task(self.async_refresh())

    async def _async_update_data(self):
        """Fetch data and calculate period cost."""

        # Summera total energi
        total_energy = 0.0
        for sensor_id in self.energy_sensors:
            energy_state = self.hass.states.get(sensor_id)
            if not energy_state or energy_state.state in ("unavailable", "unknown"):
                raise UpdateFailed(f"Energy sensor {sensor_id} unavailable")
            total_energy += float(energy_state.state)

        _LOGGER.info("Total energy from sensors: %.4f kWh", total_energy)

        # Hämta aktuell tariff
        if self.tariff_type == TARIFF_TYPE_SENSOR and self.tariff_sensor:
            tariff_state = self.hass.states.get(self.tariff_sensor)
            if not tariff_state or tariff_state.state in ("unavailable", "unknown"):
                raise UpdateFailed(f"Tariff sensor {self.tariff_sensor} unavailable")
            tariff_value = float(tariff_state.state) * self.tariff_factor
        else:
            tariff_value = self.fixed_tariff

        _LOGGER.info(
            "Current tariff: %.4f %s/kWh",
            tariff_value,
            self.entry.data.get("currency", "N/A"),
        )

        # Beräkna energi och kostnad för perioden
        period_energy = 0.0
        period_cost = 0.0

        _LOGGER.debug(
            "Last energy: %s, Last update: %s",
            self._last_energy,
            self._last_update.isoformat() if self._last_update else "N/A",
        )

        if self._last_energy is not None:
            period_energy = total_energy - self._last_energy
            period_cost = period_energy * tariff_value

            # Ackumulera kostnad
            self._accumulated_cost += period_cost

            _LOGGER.debug(
                "Period: %.4f kWh × %.4f = %.4f (total: %.4f)",
                period_energy,
                tariff_value,
                period_cost,
                self._accumulated_cost,
            )
        else:
            _LOGGER.info(
                "First update - initializing energy baseline: %.4f kWh", total_energy
            )

        # Spara för nästa iteration
        self._last_energy = total_energy
        self._last_update = dt_util.utcnow()

        _LOGGER.info("=== Update Debug ===")
        _LOGGER.info("Total energy: %.4f kWh", total_energy)
        _LOGGER.info("Last energy: %.4f kWh", self._last_energy or 0)
        _LOGGER.info("Period energy: %.4f kWh", period_energy)
        _LOGGER.info("Tariff: %.4f", tariff_value)
        _LOGGER.info("Period cost: %.4f", period_cost)
        _LOGGER.info("Accumulated cost: %.4f", self._accumulated_cost)
        _LOGGER.info("==================")

        return {
            "energy": total_energy,
            "tariff": tariff_value,
            "period_energy": period_energy,
            "period_cost": period_cost,
            "accumulated_cost": self._accumulated_cost,
        }
