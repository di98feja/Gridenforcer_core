"""Sensor platform for elcotra integration."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_CURRENCY, CONF_NAME, DOMAIN
from .coordinator import ElCoTraCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ElCoTra sensors."""
    coordinator: ElCoTraCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        ElCoTraCurrentEnergySensor(coordinator),
        ElCoTraCurrentTariffSensor(coordinator),
        ElCoTraPeriodCostSensor(coordinator),
        ElCoTraDailyCostSensor(coordinator),
        ElCoTraMonthlyCostSensor(coordinator),
        ElCoTraYearlyCostSensor(coordinator),
        ElCoTraLastMonthCostSensor(coordinator),
    ]

    async_add_entities(entities)


class ElCoTraCurrentTariffSensor(CoordinatorEntity, SensorEntity):
    """Sensor for showing current tariff."""

    def __init__(self, coordinator: ElCoTraCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._attr_name = f"{coordinator.entry.data[CONF_NAME]} Tariff"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_tariff"
        self._attr_native_unit_of_measurement = (
            f"{coordinator.entry.data[CONF_CURRENCY]}/kWh"
        )
        self._attr_icon = "mdi:cash"

    @property
    def native_value(self):
        """Return the current tariff."""
        if self.coordinator.data is None:
            return 0.0

        return self.coordinator.data.get("tariff")


class ElCoTraCurrentEnergySensor(CoordinatorEntity, SensorEntity):
    """Sensor for showing total energy."""

    def __init__(self, coordinator: ElCoTraCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_suggested_display_precision = 3

        self._attr_name = f"{coordinator.entry.data[CONF_NAME]} Energy"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_energy"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_icon = "mdi:lightning-bolt"

    @property
    def native_value(self):
        """Return the current tariff."""
        if self.coordinator.data is None:
            return 0.0
        return self.coordinator.data.get("energy")


class ElCoTraPeriodCostSensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Sensor for showing total cost."""

    _attr_should_poll = False  # Vi använder coordinator

    def __init__(self, coordinator: ElCoTraCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_suggested_display_precision = 4

        self._attr_name = f"{coordinator.entry.data[CONF_NAME]} Cost"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_cost"
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = coordinator.entry.data[CONF_CURRENCY]
        self._attr_icon = "mdi:cash-multiple"

        self._restored = False

    async def async_added_to_hass(self) -> None:
        """Restore state when entity is added to hass."""
        await super().async_added_to_hass()
        self._attr_suggested_display_precision = 2

        # First, try to restore from statistics (most reliable for imported data)
        accumulated_cost = None
        last_energy = None

        try:
            # Get the last statistic entry for this sensor
            # Must use recorder's executor for database access
            instance = get_instance(self.hass)
            stats = await instance.async_add_executor_job(
                get_last_statistics,
                self.hass,
                1,  # Only need the last entry
                self.entity_id,
                True,  # convert_units
                {"sum"},  # Only need sum
            )

            if stats and self.entity_id in stats:
                last_stat = stats[self.entity_id][0]
                if "sum" in last_stat:
                    accumulated_cost = last_stat["sum"]

                    # Also get the current energy reading to set as baseline
                    # This prevents recalculating already-accounted-for energy
                    total_energy = 0.0
                    if isinstance(self.coordinator, ElCoTraCoordinator):
                        for sensor_id in self.coordinator.energy_sensors:
                            energy_state = self.hass.states.get(sensor_id)
                            if energy_state and energy_state.state not in ("unavailable", "unknown"):
                                try:
                                    total_energy += float(energy_state.state)
                                except (ValueError, TypeError):
                                    continue

                    if total_energy > 0:
                        last_energy = total_energy
                        _LOGGER.info(
                            "Restored cost from statistics: %.4f %s at %s (current energy: %.4f kWh)",
                            accumulated_cost,
                            self._attr_native_unit_of_measurement,
                            last_stat.get("start"),
                            last_energy,
                        )
                    else:
                        _LOGGER.info(
                            "Restored cost from statistics: %.4f %s at %s (no energy baseline - will initialize on first update)",
                            accumulated_cost,
                            self._attr_native_unit_of_measurement,
                            last_stat.get("start"),
                        )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Could not restore from statistics: %s", err)

        # If no statistics, fall back to last state
        if accumulated_cost is None:
            if (
                last_state := await self.async_get_last_state()
            ) is not None and last_state.state not in ("unknown", "unavailable"):
                try:
                    accumulated_cost = float(last_state.state)

                    # Försök också återställa last_energy från attributes
                    if last_state.attributes.get("last_energy"):
                        last_energy = float(last_state.attributes["last_energy"])

                    _LOGGER.info(
                        "Restored cost from state: %.4f %s (last_energy: %.4f kWh)",
                        accumulated_cost,
                        self._attr_native_unit_of_measurement,
                        last_energy or 0,
                    )
                except (ValueError, TypeError) as err:
                    _LOGGER.warning("Could not restore cost sensor state: %s", err)

        # Apply the restored values to coordinator
        if accumulated_cost is not None and self.coordinator._last_energy is None:
            self.coordinator.restore_state(last_energy, accumulated_cost)
            self._restored = True

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # Inte tillgänglig förrän vi har data
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        )

    @property
    def native_value(self):
        """Return the current cost."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("accumulated_cost", 0.0)

    @property
    def extra_state_attributes(self):
        """Return extra attributes."""
        return {
            "last_energy": self.coordinator._last_energy,
            "last_update": self.coordinator._last_update.isoformat()
            if self.coordinator._last_update
            else None,
        }


class ElCoTraPeriodCostBaseSensor(SensorEntity):
    """Base class for period cost sensors."""

    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: ElCoTraCoordinator, period: str) -> None:
        """Initialize the sensor."""
        self.coordinator = coordinator
        self._period = period
        self._attr_native_unit_of_measurement = coordinator.entry.data[CONF_CURRENCY]
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:cash-clock"
        self._unsub_tracker = None
        # Unknown until the cost sensor has a value, rather than a misleading 0
        self._current_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()

        # Update immediately on startup
        await self._async_update_from_statistics()

        # Listen to coordinator updates to refresh statistics-based values
        self.async_on_remove(
            self.coordinator.async_add_listener(
                lambda: self.hass.async_create_task(self._async_update_from_statistics())
            )
        )

        # Schedule updates at midnight every day
        # We'll check if it's a period boundary in the callback
        self._unsub_tracker = async_track_time_change(
            self.hass, self._scheduled_update, hour=0, minute=0, second=1
        )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        if self._unsub_tracker:
            self._unsub_tracker()
            self._unsub_tracker = None

    @callback
    def _scheduled_update(self, now: datetime) -> None:
        """Handle scheduled update at midnight."""
        # Check if we should update based on period type
        should_update = False

        if self._period == "daily":
            # Always update at midnight for daily sensor
            should_update = True
        elif self._period == "monthly":
            # Only update on the 1st of the month
            should_update = now.day == 1
        elif self._period == "yearly":
            # Only update on January 1st
            should_update = now.month == 1 and now.day == 1

        if should_update:
            _LOGGER.debug("Period boundary reached for %s sensor, resetting", self._period)
            # Reset to 0 at period boundary, then update from statistics
            self._current_value = 0.0
            self.async_write_ha_state()

        # Always update the value from statistics
        self.hass.async_create_task(self._async_update_from_statistics())

    async def _async_update_from_statistics(self) -> None:
        """Update sensor value from statistics."""
        # Get the cost sensor entity_id from entity registry
        entity_registry = er.async_get(self.hass)
        cost_sensor_id = None

        for entity in entity_registry.entities.values():
            if (
                entity.config_entry_id == self.coordinator.entry.entry_id
                and entity.unique_id == f"{self.coordinator.entry.entry_id}_cost"
            ):
                cost_sensor_id = entity.entity_id
                break

        if not cost_sensor_id:
            _LOGGER.warning("Could not find cost sensor for period calculation")
            return

        # Calculate period start
        now = dt_util.now()
        if self._period == "daily":
            period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif self._period == "monthly":
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif self._period == "yearly":
            period_start = now.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        else:
            return

        try:
            # Get the baseline value from just before the period start
            # This is needed because cost is TOTAL_INCREASING (cumulative)
            instance = get_instance(self.hass)

            # Get the last statistic before the period start
            baseline_value = 0.0

            # Query statistics from a long time ago until just before period start
            # This gets us the baseline value (cumulative cost at the start of the period)
            # Use a start time far in the past to ensure we get all historical data
            very_early = period_start - timedelta(days=3650)  # 10 years ago

            baseline_stats = await instance.async_add_executor_job(
                statistics_during_period,
                self.hass,
                very_early,
                period_start,  # Until just before period start
                {cost_sensor_id},
                "hour",
                None,
                {"sum"},
            )

            if baseline_stats and cost_sensor_id in baseline_stats:
                baseline_list = baseline_stats[cost_sensor_id]
                if baseline_list:
                    # Use the last value before period start as baseline
                    last_stat = baseline_list[-1]
                    _LOGGER.debug("Raw baseline stat entry: %s", last_stat)
                    baseline_value = last_stat.get("sum", 0.0)
                    stat_time = last_stat.get("start")
                    _LOGGER.debug(
                        "Baseline for %s period: %.2f %s (from %s, %d entries)",
                        self._period,
                        baseline_value,
                        self._attr_native_unit_of_measurement,
                        dt_util.utc_from_timestamp(stat_time).isoformat() if isinstance(stat_time, (int, float)) else stat_time.isoformat() if stat_time else "unknown",
                        len(baseline_list),
                    )

            # Get the current real-time cost from the cost sensor's state
            # This ensures we use up-to-date data instead of old statistics
            cost_state = self.hass.states.get(cost_sensor_id)
            if cost_state and cost_state.state not in ("unavailable", "unknown"):
                try:
                    current_cost = float(cost_state.state)

                    # Calculate the difference from baseline
                    period_cost = current_cost - baseline_value

                    self._current_value = max(0.0, period_cost)
                    self.async_write_ha_state()

                    _LOGGER.info(
                        "Updated %s cost: %.2f %s (baseline: %.2f, current: %.2f)",
                        self._period,
                        self._current_value,
                        self._attr_native_unit_of_measurement,
                        baseline_value,
                        current_cost,
                    )
                except (ValueError, TypeError) as err:
                    _LOGGER.error(
                        "Failed to parse cost sensor state for %s: %s",
                        self._period,
                        err,
                    )
            else:
                _LOGGER.warning(
                    "Cost sensor %s unavailable for %s period calculation",
                    cost_sensor_id,
                    self._period,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error updating %s cost from statistics: %s", self._period, err)

    @property
    def native_value(self):
        """Return the current cost for this period."""
        return self._current_value

    @property
    def extra_state_attributes(self):
        """Return extra attributes."""
        return {
            "period": self._period,
        }


class ElCoTraDailyCostSensor(ElCoTraPeriodCostBaseSensor):
    """Sensor showing cost for current day."""

    def __init__(self, coordinator: ElCoTraCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "daily")
        self._attr_name = f"{coordinator.entry.data[CONF_NAME]} Cost Today"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_cost_daily"
        self._attr_icon = "mdi:calendar-today"


class ElCoTraMonthlyCostSensor(ElCoTraPeriodCostBaseSensor):
    """Sensor showing cost for current month."""

    def __init__(self, coordinator: ElCoTraCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "monthly")
        self._attr_name = f"{coordinator.entry.data[CONF_NAME]} Cost This Month"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_cost_monthly"
        self._attr_icon = "mdi:calendar-month"


class ElCoTraYearlyCostSensor(ElCoTraPeriodCostBaseSensor):
    """Sensor showing cost for current year."""

    def __init__(self, coordinator: ElCoTraCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "yearly")
        self._attr_name = f"{coordinator.entry.data[CONF_NAME]} Cost This Year"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_cost_yearly"
        self._attr_icon = "mdi:calendar"


class ElCoTraLastMonthCostSensor(SensorEntity):
    """Sensor showing the total cost of the previous calendar month."""

    _attr_should_poll = False

    def __init__(self, coordinator: ElCoTraCoordinator) -> None:
        """Initialize the sensor."""
        self.coordinator = coordinator
        self._attr_name = f"{coordinator.entry.data[CONF_NAME]} Cost Last Month"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_cost_last_month"
        self._attr_native_unit_of_measurement = coordinator.entry.data[CONF_CURRENCY]
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:calendar-arrow-left"
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        """Calculate on startup and refresh every night."""
        await super().async_added_to_hass()
        await self._async_update_from_statistics()

        # Daily rather than only on the 1st, so backfilled statistics show up.
        # 00:05 leaves time for the recorder to compile the last hour.
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._scheduled_update, hour=0, minute=5, second=0
            )
        )

    @callback
    def _scheduled_update(self, now: datetime) -> None:
        """Handle the nightly refresh."""
        self.hass.async_create_task(self._async_update_from_statistics())

    async def _async_update_from_statistics(self) -> None:
        """Read last month's cost change from the recorder's monthly statistics."""
        cost_sensor_id = er.async_get(self.hass).async_get_entity_id(
            "sensor", DOMAIN, f"{self.coordinator.entry.entry_id}_cost"
        )
        if not cost_sensor_id:
            _LOGGER.warning("Could not find cost sensor for last month calculation")
            return

        this_month = dt_util.now().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        last_month = (this_month - timedelta(days=1)).replace(day=1)

        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            last_month,
            this_month,
            {cost_sensor_id},
            "month",
            None,
            {"change"},
        )
        rows = stats.get(cost_sensor_id)
        self._attr_native_value = rows[0].get("change") if rows else None
        self.async_write_ha_state()

        _LOGGER.debug(
            "Last month (%s) cost: %s", last_month.strftime("%Y-%m"), self.native_value
        )
