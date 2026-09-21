"""Constants for the ElCoTra, Electricity Cost Tracker component."""

DOMAIN = "elcotra"
CONF_NAME = "name"
CONF_ENERGY_SENSORS = "energy_sensors"
CONF_TARIFF_SENSOR = "tariff_sensor"
CONF_TARIFF_TYPE = "tariff_type"
CONF_FIXED_TARIFF = "fixed_tariff"
DEFAULT_FIXED_TARIFF = 0.0
# Multiplier applied to the tariff sensor value, e.g. 11 to turn EUR into SEK
CONF_TARIFF_FACTOR = "tariff_factor"
DEFAULT_TARIFF_FACTOR = 1.0
CONF_CURRENCY = "currency"
DEFAULT_CURRENCY = "SEK"
CURRENCIES = ["SEK", "EUR", "USD", "GBP", "DKK", "NOK"]
TARIFF_TYPE_FIXED = "fixed"
TARIFF_TYPE_SENSOR = "sensor"
DEFAULT_TARIFF_TYPE = TARIFF_TYPE_SENSOR
UPDATE_INTERVAL_SECONDS = 900  # 15 minutes

# CSV Import constants
SERVICE_IMPORT_CSV_DATA = "import_csv_data"
CONF_CSV_URLS = "csv_urls"
CONF_CSV_URL_L1 = "csv_url_l1"
CONF_CSV_URL_L2 = "csv_url_l2"
CONF_CSV_URL_L3 = "csv_url_l3"
CONF_AUTO_IMPORT_ON_SETUP = "auto_import_on_setup"
CONF_TARGET_SENSORS = "target_sensors"
CONF_IMPORT_RANGE = "import_range"
CONF_IMPORT_OPTIONS = "options"
CONF_APPLY_TO_COST = "apply_to_cost_sensors"
CONF_RECALCULATE_STATS = "recalculate_statistics"
CONF_BACKUP_EXISTING = "backup_existing"
CONF_FULL_REIMPORT = "full_reimport"
CONF_START_DATE = "start"
CONF_END_DATE = "end"

# Energy offset storage keys
CONF_ENERGY_OFFSETS = "energy_offsets"
CONF_COST_OFFSETS = "cost_offsets"
OFFSET_VALUE = "value"
OFFSET_IMPORTED_AT = "imported_at"
OFFSET_SOURCE = "source"

# Phases
PHASE_L1 = "l1"
PHASE_L2 = "l2"
PHASE_L3 = "l3"
PHASES = [PHASE_L1, PHASE_L2, PHASE_L3]
