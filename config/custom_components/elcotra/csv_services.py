"""CSV import service handlers for ElCoTra integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    StatisticData,
    StatisticMetaData,
    async_import_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    CONF_APPLY_TO_COST,
    CONF_BACKUP_EXISTING,
    CONF_COST_OFFSETS,
    CONF_CSV_URLS,
    CONF_CURRENCY,
    CONF_END_DATE,
    CONF_ENERGY_OFFSETS,
    CONF_FIXED_TARIFF,
    CONF_FULL_REIMPORT,
    CONF_IMPORT_OPTIONS,
    CONF_IMPORT_RANGE,
    CONF_RECALCULATE_STATS,
    CONF_START_DATE,
    CONF_TARGET_SENSORS,
    CONF_TARIFF_FACTOR,
    CONF_TARIFF_SENSOR,
    CONF_TARIFF_TYPE,
    DEFAULT_TARIFF_FACTOR,
    DOMAIN,
    OFFSET_IMPORTED_AT,
    OFFSET_SOURCE,
    OFFSET_VALUE,
    PHASES,
    SERVICE_IMPORT_CSV_DATA,
)
from .csv_importer import CSVImportError, ShellyCSVImporter

_LOGGER = logging.getLogger(__name__)

# Service schema for import_csv_data
IMPORT_CSV_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CSV_URLS): vol.Schema({vol.In(PHASES): cv.url}),
        vol.Required(CONF_TARGET_SENSORS): vol.Schema({vol.In(PHASES): cv.entity_id}),
        vol.Optional(CONF_IMPORT_RANGE): vol.Schema(
            {
                vol.Optional(CONF_START_DATE): cv.datetime,
                vol.Optional(CONF_END_DATE): cv.datetime,
            }
        ),
        vol.Optional(CONF_IMPORT_OPTIONS): vol.Schema(
            {
                vol.Optional(CONF_APPLY_TO_COST, default=True): cv.boolean,
                vol.Optional(CONF_RECALCULATE_STATS, default=True): cv.boolean,
                vol.Optional(CONF_BACKUP_EXISTING, default=True): cv.boolean,
                vol.Optional(CONF_FULL_REIMPORT, default=False): cv.boolean,
            }
        ),
    }
)


async def _async_get_import_baseline(
    hass: HomeAssistant, statistic_id: str
) -> tuple[datetime | None, float]:
    """Return the incremental import baseline for a sensor.

    Returns a tuple of (start of the last recorded statistics hour, cumulative
    sum BEFORE that hour). The last hour may have been computed from a partial
    CSV, so imports should re-process rows from that hour onwards and rebuild
    its statistic, continuing the sum from the hour before it.
    """
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 2, statistic_id, True, {"sum"}
    )
    stats = rows.get(statistic_id)
    if not stats:
        return None, 0.0

    high_water = dt_util.utc_from_timestamp(stats[0]["start"])
    seed = 0.0
    if len(stats) > 1 and stats[1]["sum"] is not None:
        seed = stats[1]["sum"]
    return high_water, seed


async def _async_get_sum_before(
    hass: HomeAssistant, statistic_id: str, cutoff: datetime
) -> float:
    """Return the last recorded cumulative sum strictly before a point in time."""
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 2, statistic_id, True, {"sum"}
    )
    for stat in rows.get(statistic_id, []):  # newest first
        if (
            dt_util.utc_from_timestamp(stat["start"]) < cutoff
            and stat["sum"] is not None
        ):
            return stat["sum"]
    return 0.0


def _find_cost_sensor_id(hass: HomeAssistant, entry_id: str) -> str | None:
    """Find the cost sensor entity_id for a config entry."""
    entity_registry = er.async_get(hass)
    for entity in entity_registry.entities.values():
        if (
            entity.domain == "sensor"
            and entity.unique_id
            and entity.unique_id.endswith("_cost")
            and entity.config_entry_id == entry_id
        ):
            return entity.entity_id
    return None


def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for Gridenforcer Core integration."""
    # Only register service once
    if hass.services.has_service(DOMAIN, SERVICE_IMPORT_CSV_DATA):
        return

    async def handle_import_csv_data(call: ServiceCall) -> ServiceResponse:
        """Handle the import_csv_data service call.

        Args:
            call: Service call with CSV URLs and target sensors

        Returns:
            Service response with import results

        Raises:
            ServiceValidationError: If validation fails
            HomeAssistantError: If import fails
        """
        csv_urls: dict[str, str] = call.data[CONF_CSV_URLS]
        target_sensors: dict[str, str] = call.data[CONF_TARGET_SENSORS]
        import_range: dict[str, datetime] | None = call.data.get(CONF_IMPORT_RANGE)
        options: dict[str, Any] = call.data.get(CONF_IMPORT_OPTIONS, {})

        # Validate that phases match between URLs and sensors
        if set(csv_urls.keys()) != set(target_sensors.keys()):
            raise ServiceValidationError(
                "CSV URLs and target sensors must specify the same phases"
            )

        # Extract date range
        start_date = None
        end_date = None
        if import_range:
            start_date = import_range.get(CONF_START_DATE)
            end_date = import_range.get(CONF_END_DATE)

            if start_date and end_date and end_date < start_date:
                raise ServiceValidationError("End date must be after start date")

        # Validate target sensors exist
        entity_registry = er.async_get(hass)
        for phase, entity_id in target_sensors.items():
            if not entity_registry.async_get(entity_id):
                raise ServiceValidationError(
                    f"Target sensor {entity_id} for phase {phase} does not exist"
                )

        _LOGGER.info(
            "Starting CSV import for phases: %s",
            ", ".join(csv_urls.keys()),
        )

        # Incremental import is the default: only process CSV rows newer than
        # what the recorder already has. A full re-import can be forced with
        # the full_reimport option or by passing an explicit import_range.
        incremental = not options.get(CONF_FULL_REIMPORT, False) and not import_range

        start_dates: dict[str, datetime] = {}
        seed_sums: dict[str, float] = {}
        if incremental:
            for phase, entity_id in target_sensors.items():
                high_water, seed = await _async_get_import_baseline(hass, entity_id)
                if high_water:
                    start_dates[phase] = high_water
                    seed_sums[entity_id] = seed
                    _LOGGER.debug(
                        "Phase %s: importing from %s (sum before: %.2f kWh)",
                        phase,
                        high_water.isoformat(),
                        seed,
                    )

        # Create importer
        importer = ShellyCSVImporter(hass)

        # Import data from all phases
        try:
            import_results = await importer.import_multiple_phases(
                csv_urls,
                start_date=start_date,
                end_date=end_date,
                start_dates=start_dates or None,
            )
        except CSVImportError as err:
            raise HomeAssistantError(f"Failed to import CSV data: {err}") from err

        # Process import results
        offsets: dict[str, dict[str, Any]] = {}
        statistics_data: dict[str, list[StatisticData]] = {}
        hourly_deltas: dict[str, dict[datetime, float]] = {}

        for phase, result in import_results.items():
            entity_id = target_sensors[phase]

            if not result.data_points:
                _LOGGER.info("Phase %s: no new data since last import", phase)
                continue

            _LOGGER.debug(
                "Phase %s data range: %s to %s (%d points)",
                phase,
                result.data_points[0].timestamp,
                result.data_points[-1].timestamp,
                len(result.data_points),
            )

            # Aggregate data per hour (HA statistics require hourly timestamps)
            hourly_data: dict[datetime, float] = {}

            for data_point in result.data_points:
                # Round timestamp to start of hour
                hour_start = data_point.timestamp.replace(
                    minute=0, second=0, microsecond=0
                )

                # Accumulate energy for this hour
                if hour_start not in hourly_data:
                    hourly_data[hour_start] = 0.0
                hourly_data[hour_start] += data_point.energy_wh / 1000.0

            # Build cumulative statistics, continuing from the recorded sum
            stats = []
            seed_kwh = seed_sums.get(entity_id, 0.0)
            cumulative_kwh = seed_kwh

            for hour_start in sorted(hourly_data.keys()):
                hour_energy = hourly_data[hour_start]
                cumulative_kwh += hour_energy

                stats.append(
                    StatisticData(
                        start=hour_start,
                        state=cumulative_kwh,  # Cumulative total (always increasing)
                        sum=cumulative_kwh,  # Same as state for total_increasing
                    )
                )

            statistics_data[entity_id] = stats
            hourly_deltas[entity_id] = hourly_data

            # Store offset for this phase (total cumulative energy)
            offsets[phase] = {
                OFFSET_VALUE: cumulative_kwh,
                OFFSET_IMPORTED_AT: datetime.now().isoformat(),
                OFFSET_SOURCE: result.source_url,
            }

            _LOGGER.info(
                "Aggregated %d readings into %d hourly statistics for phase %s (%.2f → %.2f kWh)",
                len(result.data_points),
                len(stats),
                phase,
                seed_kwh,
                cumulative_kwh,
            )

        if not statistics_data:
            _LOGGER.info("CSV import: no new data for any phase, nothing to do")
            return {
                "success": True,
                "phases_imported": [],
                "total_energy_kwh": 0.0,
                "cost_offset": 0.0,
                "data_points": 0,
            }

        # Update config entry with offsets
        await _store_offsets(hass, offsets)

        # Calculate cost statistics for the imported hours if requested
        cost_statistics_data: list[StatisticData] = []
        cost_offset = 0.0
        config_entries = hass.config_entries.async_entries(DOMAIN)
        cost_sensor_id = (
            _find_cost_sensor_id(hass, config_entries[0].entry_id)
            if config_entries
            else None
        )
        if options.get(CONF_APPLY_TO_COST, True):
            (
                cost_statistics_data,
                cost_offset,
            ) = await _calculate_historical_cost_from_import(
                hass,
                hourly_deltas,
                cost_sensor_id,
                incremental,
            )

        # Import statistics to recorder if requested
        if options.get(CONF_RECALCULATE_STATS, True):
            # async_import_statistics updates existing hours in place, so
            # re-imported hours (e.g. the partial last hour) are corrected
            # rather than duplicated
            _LOGGER.info("Importing statistics (existing hours will be updated)")

            # Import the new statistics
            await _import_statistics(hass, statistics_data, target_sensors)

            # Import cost statistics
            if cost_statistics_data and cost_sensor_id:
                await _import_cost_statistics(
                    hass, cost_statistics_data, cost_sensor_id
                )

                # Update coordinator with the new accumulated cost
                await _update_coordinator_after_import(
                    hass, cost_offset, target_sensors
                )

        # Store cost offset
        if options.get(CONF_APPLY_TO_COST, True):
            await _store_cost_offset(hass, cost_offset)

        _LOGGER.info(
            "CSV import completed successfully for %d phases",
            len(import_results),
        )

        # Return summary
        return {
            "success": True,
            "phases_imported": list(offsets.keys()),
            "total_energy_kwh": sum(
                r.total_energy_kwh for r in import_results.values()
            ),
            "cost_offset": cost_offset,
            "data_points": sum(len(r.data_points) for r in import_results.values()),
        }

    # Register service
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_CSV_DATA,
        handle_import_csv_data,
        schema=IMPORT_CSV_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    _LOGGER.info("Registered service: %s", SERVICE_IMPORT_CSV_DATA)


async def _store_offsets(
    hass: HomeAssistant,
    offsets: dict[str, dict[str, Any]],
) -> None:
    """Store energy offsets in config entry.

    Args:
        hass: Home Assistant instance
        offsets: Dictionary of phase offsets
    """
    # Find the config entry for this integration
    config_entries = hass.config_entries.async_entries(DOMAIN)
    if not config_entries:
        _LOGGER.warning("No config entry found to store offsets")
        return

    entry = config_entries[0]  # Assume single config entry

    # Update config entry data
    new_data = dict(entry.data)
    new_data[CONF_ENERGY_OFFSETS] = offsets

    hass.config_entries.async_update_entry(entry, data=new_data)

    _LOGGER.info("Stored energy offsets for phases: %s", ", ".join(offsets.keys()))


async def _store_cost_offset(
    hass: HomeAssistant,
    cost_offset: float,
) -> None:
    """Store cost offset in config entry.

    Args:
        hass: Home Assistant instance
        cost_offset: Total historical cost
    """
    config_entries = hass.config_entries.async_entries(DOMAIN)
    if not config_entries:
        _LOGGER.warning("No config entry found to store cost offset")
        return

    entry = config_entries[0]

    # Update config entry data
    new_data = dict(entry.data)
    new_data[CONF_COST_OFFSETS] = {
        "total": {
            OFFSET_VALUE: cost_offset,
            OFFSET_IMPORTED_AT: datetime.now().isoformat(),
        }
    }

    hass.config_entries.async_update_entry(entry, data=new_data)

    _LOGGER.info("Stored cost offset: %.2f", cost_offset)


async def _clear_existing_statistics(
    hass: HomeAssistant,
    target_sensors: dict[str, str],
    clear_cost_sensor: bool = True,
) -> None:
    """Clear existing statistics for target sensors.

    This prevents discontinuity between old sensor history and new imported statistics.

    Note: With large databases, clearing can take a long time. This function
    initiates the clear operation but doesn't wait for completion to avoid timeouts.

    Args:
        hass: Home Assistant instance
        target_sensors: Dictionary mapping phases to entity IDs
        clear_cost_sensor: If True, also clear the cost sensor statistics
    """
    from homeassistant.components.recorder import get_instance

    statistic_ids = list(target_sensors.values())

    # Also find and clear the cost sensor if requested
    if clear_cost_sensor:
        # Find the elcotra config entry to get the cost sensor entity_id
        config_entries = hass.config_entries.async_entries(DOMAIN)
        if config_entries:
            entry = config_entries[0]
            cost_sensor_id = f"sensor.{entry.entry_id}_cost"

            # Check if entity exists in registry
            entity_registry = er.async_get(hass)
            if cost_entity := entity_registry.async_get(cost_sensor_id):
                statistic_ids.append(cost_entity.entity_id)
                _LOGGER.info("Will also clear cost sensor: %s", cost_entity.entity_id)
            else:
                # Try to find cost sensor by searching for entities with "_cost" suffix
                for entity in entity_registry.entities.values():
                    if (
                        entity.domain == "sensor"
                        and entity.unique_id
                        and entity.unique_id.endswith("_cost")
                        and entity.config_entry_id == entry.entry_id
                    ):
                        statistic_ids.append(entity.entity_id)
                        _LOGGER.info(
                            "Found and will clear cost sensor: %s", entity.entity_id
                        )
                        break

    _LOGGER.info(
        "Clearing existing statistics for %d sensors: %s",
        len(statistic_ids),
        ", ".join(statistic_ids),
    )

    # Clear all sensors in a single operation - more efficient
    done_event = asyncio.Event()

    def clear_done() -> None:
        hass.loop.call_soon_threadsafe(done_event.set)

    # Clear all statistics in one call
    _LOGGER.info("Initiating clear operation for all sensors")
    get_instance(hass).async_clear_statistics(statistic_ids, on_done=clear_done)

    # Wait for clearing to complete, with a reasonable timeout
    # For large databases, this might take a while
    try:
        _LOGGER.info("Waiting for statistics clearing to complete (timeout: 60s)")
        async with asyncio.timeout(60):
            await done_event.wait()
        _LOGGER.info(
            "Successfully cleared statistics for %d sensors", len(statistic_ids)
        )
    except TimeoutError:
        _LOGGER.warning(
            "Statistics clearing timed out after 60 seconds. "
            "Proceeding with import anyway",
        )
        # Give it a bit more time for pending operations
        await asyncio.sleep(5)


async def _import_statistics(
    hass: HomeAssistant,
    statistics_data: dict[str, list[StatisticData]],
    target_sensors: dict[str, str],
) -> None:
    """Import statistics to Home Assistant recorder.

    Existing hours are updated in place by async_import_statistics, so no
    duplicate filtering is needed.

    Args:
        hass: Home Assistant instance
        statistics_data: Dictionary mapping entity IDs to statistics
        target_sensors: Dictionary mapping phases to entity IDs
    """
    for phase, entity_id in target_sensors.items():
        stats = statistics_data.get(entity_id)
        if not stats:
            continue

        metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"Energy {phase.upper()}",
            source="recorder",
            statistic_id=entity_id,
            unit_of_measurement="kWh",
            mean_type=StatisticMeanType.NONE,
        )

        async_import_statistics(hass, metadata, stats)

        _LOGGER.info("Sensor %s: imported %d hourly statistics", entity_id, len(stats))

    _LOGGER.info(
        "Completed statistics import for %d sensors",
        len(statistics_data),
    )


async def _calculate_historical_cost_from_import(
    hass: HomeAssistant,
    hourly_deltas: dict[str, dict[datetime, float]],
    cost_sensor_id: str | None,
    incremental: bool,
) -> tuple[list[StatisticData], float]:
    """Calculate historical cost from imported energy data.

    Calculates costs directly from the imported per-hour energy deltas and
    historical tariff data.

    Args:
        hass: Home Assistant instance
        hourly_deltas: Dictionary mapping entity IDs to per-hour energy (kWh)
        cost_sensor_id: Entity ID of the cost sensor, for seeding the sum
        incremental: If True, continue the cost sum from recorded statistics

    Returns:
        Tuple of (cost statistics list, total cost)
    """
    from homeassistant.components.recorder.history import get_significant_states

    # Find the elcotra config entry
    config_entries = hass.config_entries.async_entries(DOMAIN)
    if not config_entries:
        _LOGGER.error("No elcotra config entry found for cost calculation")
        return [], 0.0

    entry = config_entries[0]

    # Get configuration from entry
    tariff_type = entry.data.get(CONF_TARIFF_TYPE, "fixed")
    fixed_tariff = entry.data.get(CONF_FIXED_TARIFF, 0.0)
    tariff_sensor = entry.data.get(CONF_TARIFF_SENSOR)
    tariff_factor = entry.data.get(CONF_TARIFF_FACTOR, DEFAULT_TARIFF_FACTOR)
    currency = entry.data.get(CONF_CURRENCY, "SEK")

    # Combine all phases' energy by hour
    hourly_energy: dict[datetime, float] = {}
    for deltas in hourly_deltas.values():
        for hour_start, energy_kwh in deltas.items():
            if hour_start not in hourly_energy:
                hourly_energy[hour_start] = 0.0
            hourly_energy[hour_start] += energy_kwh

    if not hourly_energy:
        _LOGGER.warning("No energy data to calculate costs from")
        return [], 0.0

    # Sort hours
    sorted_hours = sorted(hourly_energy.keys())
    earliest = sorted_hours[0]
    latest = sorted_hours[-1]

    _LOGGER.info(
        "Calculating historical costs from %s to %s (%d hours)",
        earliest.isoformat(),
        latest.isoformat(),
        len(sorted_hours),
    )
    _LOGGER.info("Tariff type: %s", tariff_type)

    # Fetch tariff data if using sensor
    # First try statistics (more reliable for historical data), then fall back to history
    tariff_data: dict[datetime, float] = {}
    if tariff_type == "sensor" and tariff_sensor:
        _LOGGER.info("Fetching tariff data for %s", tariff_sensor)

        # Try to get statistics first (preferred for historical data)
        try:
            _LOGGER.info(
                "Attempting to fetch tariff statistics from %s to %s",
                earliest.isoformat(),
                latest.isoformat(),
            )
            stats = await get_instance(hass).async_add_executor_job(
                statistics_during_period,
                hass,
                earliest,
                latest + timedelta(hours=1),
                {tariff_sensor},
                "hour",
                None,
                {"mean"},
            )

            if stats and tariff_sensor in stats:
                for stat in stats[tariff_sensor]:
                    try:
                        tariff_value = stat.get("mean")
                        stat_time = stat.get("start")

                        if tariff_value is not None and stat_time is not None:
                            # Convert timestamp to datetime if needed
                            if isinstance(stat_time, (int, float)):
                                hour_start = dt_util.utc_from_timestamp(stat_time)
                            else:
                                hour_start = stat_time

                            # Ensure hour_start is rounded to the hour
                            hour_start = hour_start.replace(
                                minute=0, second=0, microsecond=0
                            )
                            tariff_data[hour_start] = float(tariff_value)
                    except (ValueError, TypeError, AttributeError) as err:
                        _LOGGER.debug("Skipping invalid tariff statistic: %s", err)
                        continue

                _LOGGER.info(
                    "Loaded %d tariff values from statistics", len(tariff_data)
                )
            else:
                _LOGGER.info(
                    "No tariff statistics found for %s, will try history", tariff_sensor
                )
        except Exception as e:
            _LOGGER.warning("Error fetching tariff statistics: %s, will try history", e)

        # Fall back to history if no statistics found
        if not tariff_data:
            try:
                _LOGGER.info("Fetching tariff history for %s", tariff_sensor)
                states = await get_instance(hass).async_add_executor_job(
                    get_significant_states,
                    hass,
                    earliest,
                    latest + timedelta(hours=1),
                    [tariff_sensor],
                )

                if states and tariff_sensor in states:
                    for state in states[tariff_sensor]:
                        try:
                            tariff_value = float(state.state)
                            # Use state's last_changed or last_updated
                            timestamp = (
                                state.last_changed
                                if hasattr(state, "last_changed")
                                else dt_util.parse_datetime(state.get("last_changed"))
                            )
                            if timestamp:
                                # Round to hour
                                hour_start = timestamp.replace(
                                    minute=0, second=0, microsecond=0
                                )
                                tariff_data[hour_start] = tariff_value
                        except (ValueError, TypeError, AttributeError):
                            continue

                    _LOGGER.info(
                        "Loaded %d tariff values from history", len(tariff_data)
                    )
                else:
                    _LOGGER.warning("No tariff history found for %s", tariff_sensor)
            except Exception as e:
                _LOGGER.error("Error fetching tariff history: %s", e)

        # Log the coverage
        if tariff_data:
            tariff_hours = sorted(tariff_data.keys())
            _LOGGER.info(
                "Tariff data coverage: %s to %s (%d hours)",
                tariff_hours[0].isoformat(),
                tariff_hours[-1].isoformat(),
                len(tariff_hours),
            )

    # Continue the cost sum from what the recorder already has
    cost_seed = 0.0
    if incremental and cost_sensor_id:
        cost_seed = await _async_get_sum_before(hass, cost_sensor_id, earliest)
        if cost_seed:
            _LOGGER.debug("Continuing cost sum from %.2f %s", cost_seed, currency)

    # Calculate cost for each hour. Walk the sorted tariff schedule with a
    # single index instead of rescanning it for every hour
    cost_statistics: list[StatisticData] = []
    cumulative_cost = cost_seed
    total_energy = 0.0

    tariff_schedule = sorted(tariff_data.items())
    tariff_idx = 0
    current_tariff = fixed_tariff

    for hour_start in sorted_hours:
        energy_kwh = hourly_energy[hour_start]
        total_energy += energy_kwh

        # Get tariff for this hour
        if tariff_type == "sensor" and tariff_schedule:
            # Advance to the most recent tariff before or at this hour
            while (
                tariff_idx < len(tariff_schedule)
                and tariff_schedule[tariff_idx][0] <= hour_start
            ):
                current_tariff = tariff_schedule[tariff_idx][1] * tariff_factor
                tariff_idx += 1
            tariff = current_tariff
        else:
            tariff = fixed_tariff

        # Calculate cost for this hour
        hour_cost = energy_kwh * tariff
        cumulative_cost += hour_cost

        # Create statistic entry
        cost_statistics.append(
            StatisticData(
                start=hour_start,
                state=cumulative_cost,
                sum=cumulative_cost,
            )
        )

    _LOGGER.info(
        "Historical cost calculation completed: %.2f %s from %.2f kWh (%d hourly entries)",
        cumulative_cost - cost_seed,
        currency,
        total_energy,
        len(cost_statistics),
    )

    return cost_statistics, cumulative_cost


async def _import_cost_statistics(
    hass: HomeAssistant,
    cost_statistics: list[StatisticData],
    cost_sensor_id: str,
) -> None:
    """Import cost statistics to recorder.

    Existing hours are updated in place by async_import_statistics, so no
    duplicate filtering is needed.

    Args:
        hass: Home Assistant instance
        cost_statistics: List of cost statistics to import
        cost_sensor_id: Entity ID of the cost sensor
    """
    if not cost_statistics:
        return

    config_entries = hass.config_entries.async_entries(DOMAIN)
    if not config_entries:
        _LOGGER.error("No elcotra config entry found")
        return

    currency = config_entries[0].data.get(CONF_CURRENCY, "SEK")

    metadata = StatisticMetaData(
        has_mean=False,
        has_sum=True,
        name="Cost",
        source="recorder",
        statistic_id=cost_sensor_id,
        unit_of_measurement=currency,
        mean_type=StatisticMeanType.NONE,
    )

    async_import_statistics(hass, metadata, cost_statistics)

    _LOGGER.info(
        "Imported %d cost statistics entries for %s",
        len(cost_statistics),
        cost_sensor_id,
    )


async def _update_coordinator_after_import(
    hass: HomeAssistant,
    accumulated_cost: float,
    target_sensors: dict[str, str],
) -> None:
    """Update coordinator state after importing statistics.

    This ensures the coordinator continues from the imported values
    instead of starting from zero.

    Args:
        hass: Home Assistant instance
        accumulated_cost: Total accumulated cost from import
        target_sensors: Dictionary mapping phases to entity IDs
    """
    # Find the elcotra config entry
    config_entries = hass.config_entries.async_entries(DOMAIN)
    if not config_entries:
        _LOGGER.error("No elcotra config entry found")
        return

    entry = config_entries[0]

    # Get coordinator from hass.data
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("No coordinator found in hass.data")
        return

    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Get current total energy from sensors
    total_energy = 0.0
    for entity_id in target_sensors.values():
        energy_state = hass.states.get(entity_id)
        if energy_state and energy_state.state not in ("unavailable", "unknown"):
            try:
                total_energy += float(energy_state.state)
            except (ValueError, TypeError):
                continue

    # Update coordinator state
    coordinator.restore_state(total_energy, accumulated_cost)

    _LOGGER.info(
        "Updated coordinator after import: last_energy=%.4f kWh, accumulated_cost=%.4f SEK",
        total_energy,
        accumulated_cost,
    )

    # Trigger an immediate update to verify everything works
    _LOGGER.info("Triggering immediate coordinator update to verify import")
    await coordinator.async_refresh()
    _LOGGER.info(
        "Immediate update completed. Current accumulated cost: %.4f SEK",
        coordinator._accumulated_cost,
    )

    # Also update period sensors (daily, monthly, yearly)
    await _update_period_sensors_after_import(hass, entry.entry_id)


async def _update_period_sensors_after_import(
    hass: HomeAssistant,
    entry_id: str,
) -> None:
    """Trigger update of period sensors after CSV import.

    Period sensors (daily, monthly, yearly) need to recalculate from
    statistics after new data is imported.

    Args:
        hass: Home Assistant instance
        entry_id: Config entry ID
    """
    # Wait a bit for statistics to be committed to database
    _LOGGER.info("Waiting 2 seconds for statistics to be committed to database")
    await asyncio.sleep(2)

    entity_registry = er.async_get(hass)

    # Find all period sensors for this config entry
    period_sensor_suffixes = ["_cost_daily", "_cost_monthly", "_cost_yearly"]

    for suffix in period_sensor_suffixes:
        unique_id = f"{entry_id}{suffix}"

        # Find entity by unique_id
        for entity in entity_registry.entities.values():
            if entity.unique_id == unique_id:
                entity_id = entity.entity_id

                # Get the entity object and trigger update
                if entity_obj := hass.data["entity_components"]["sensor"].get_entity(
                    entity_id
                ):
                    _LOGGER.info("Updating period sensor: %s", entity_id)
                    # Call the update method directly
                    await entity_obj._async_update_from_statistics()
                break
