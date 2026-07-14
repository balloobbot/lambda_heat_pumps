"""Data update coordinator for Lambda."""

from __future__ import annotations
from datetime import timedelta
import logging
import os
import yaml
import json
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.event import async_track_time_interval
from modbus_connection import ModbusError
from modbus_connection.pymodbus import connect_tcp
from .const import (
    SENSOR_TYPES,
    HP_SENSOR_TEMPLATES,
    BOIL_SENSOR_TEMPLATES,
    BUFF_SENSOR_TEMPLATES,
    SOL_SENSOR_TEMPLATES,
    HC_SENSOR_TEMPLATES,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_FAST_UPDATE_INTERVAL,
    CALCULATED_SENSOR_TEMPLATES,
    LAMBDA_MODBUS_UNIT_ID,
    LAMBDA_MODBUS_PORT,
)
from .lambda_modbus import LambdaHeatPump
from .lambda_modbus.ranges import base_address
from .utils import (
    load_disabled_registers,
    is_register_disabled,
    increment_cycling_counter,
    get_firmware_version_int,
    get_compatible_sensors,
    normalize_name_prefix,
    slugify_name_prefix_for_lookup,
    generate_sensor_names,
    resolve_entity_id_by_unique_id,
    detect_sensor_change,
    get_stored_sensor_id,
    store_sensor_id,
    get_stored_thermal_sensor_id,
    store_thermal_sensor_id,
    is_sentinel_value,
)
import time

# Which lambda_modbus component backs each group of register templates, and how a
# template key maps onto a field name on it. The general sensors are one flat
# dict keyed by a prefixed name (ambient_temperature); the per-module templates
# are keyed by the bare field name.
GENERAL_PREFIXES = {"ambient_": "ambient", "emgr_": "e_manager"}
MODULE_TEMPLATES = {
    "hp": (HP_SENSOR_TEMPLATES, "heat_pumps"),
    "boil": (BOIL_SENSOR_TEMPLATES, "boilers"),
    "buff": (BUFF_SENSOR_TEMPLATES, "buffers"),
    "sol": (SOL_SENSOR_TEMPLATES, "solar_modules"),
    "hc": (HC_SENSOR_TEMPLATES, "heating_circuits"),
}

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)

# sensor_id (Schlüssel aus HP_SENSOR_TEMPLATES) je Energie-Typ. Wird gebraucht, um die
# unique_id unserer eigenen Modbus-Energiesensoren zu rekonstruieren und darüber die
# tatsächliche entity_id aus der Entity Registry aufzulösen.
INTERNAL_ENERGY_SENSOR_IDS = {
    "electrical": "compressor_power_consumption_accumulated",
    "thermal": "compressor_thermal_energy_output_accumulated",
}

# Sensor-Wechsel-Erkennung läuft bei jedem Start, um alle Sensor-Wechsel zu erkennen


class LambdaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Lambda data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Initialize."""
        # Lese update_interval aus den Optionen, falls vorhanden
        update_interval = entry.options.get("update_interval", DEFAULT_UPDATE_INTERVAL)
        _LOGGER.debug("Update interval from options: %s seconds", update_interval)
        _LOGGER.debug("Entry options: %s", entry.options)
        _LOGGER.debug(
            "Room thermostat control: %s",
            entry.options.get("room_thermostat_control", "nicht gefunden"),
        )

        super().__init__(
            hass,
            _LOGGER,
            name="Lambda Coordinator",
            update_interval=timedelta(seconds=update_interval),
        )
        self.host = entry.data["host"]
        self.port = entry.data.get("port", LAMBDA_MODBUS_PORT)
        self.slave_id = entry.data.get("slave_id", LAMBDA_MODBUS_UNIT_ID)
        self.debug_mode = entry.data.get("debug_mode", False)
        if self.debug_mode:
            _LOGGER.setLevel(logging.DEBUG)
        # The Modbus link, the handle for our unit on it, and the device model
        # over that handle. The connection is ours to close; the unit is what
        # everything downstream reads and writes through.
        self.connection = None
        self.unit = None
        self.device = None
        # Both are loaded from lambda_wp_config.yaml in async_init.
        self.disabled_registers = set()
        self.sensor_overrides = {}
        self.config_entry_id = entry.entry_id
        self._config_dir = hass.config.config_dir
        self._config_path = os.path.join(self._config_dir, "lambda_heat_pumps")
        self.hass = hass
        self.entry = entry
        self._last_operating_state = {}
        self._energy_last_operating_state = {}  # Separate state for energy attribution (full update only)
        self._last_compressor_rating = {}  # Für compressor_unit_rating Flankenerkennung (0 → >0)
        self._last_state = {}  # HP_STATE (reg 1002) – persisted across restarts
        self._heating_cycles = {}
        self._heating_energy = {}
        self._last_energy_update = {}
        self._cycling_offsets = {}
        self._energy_offsets = {}
        
        # Energy Consumption Tracking
        self._last_energy_reading = {}  # {hp_index: last_kwh_value}
        self._energy_consumption = {}   # {hp_index: {mode: {period: value}}}
        self._energy_sensor_configs = {}  # Sensor-Konfigurationen aus Config (optional)
        self._sensor_ids = {}  # {hp_index: sensor_entity_id} für Sensor-Wechsel-Erkennung (elektrisch)
        self._thermal_sensor_ids = {}  # {hp_index: sensor_entity_id} für Thermik-Sensor-Wechsel-Erkennung
        self._energy_sensor_states = {}  # {entity_id: {state, energy_value, yesterday_value, ...}} aus cycle_energy_persist
        self._energy_unit_cache = {}  # {hp_index: unit_string} - Memory-only cache for performance
        self._energy_first_value_seen = {}  # {hp_index: bool} - In-Memory Flag für Zero-Value Protection
        self._last_thermal_energy_reading = {}  # {hp_index: last_kwh_value} for thermal
        self._thermal_energy_first_value_seen = {}  # {hp_index: bool} for thermal
        self._sensor_detection_executed = False  # Flag to prevent multiple sensor detection runs
        self._use_legacy_names = entry.data.get("use_legacy_modbus_names", True)
        self._name_prefix = entry.data.get("name", "eu08l")
        self._persist_file = os.path.join(
            self._config_path, "cycle_energy_persist.json"
        )

        # Adressen der Entities, die sensor.py registriert. Das Lesen richtet
        # sich nicht mehr danach — lambda_modbus plant die Blöcke aus dem
        # Registermodell — aber sensor.py pflegt die Menge weiterhin.
        self._enabled_addresses = set()
        self._entity_addresses = {}  # Mapping entity_id -> address from sensors

        # Int32 Register Order Support (Issue #22)
        self._int32_register_order = "high_first"  # Default value

        # Dynamische Cycling-Sensor-Meldungen
        self._cycling_warnings = {}  # Dict: entity_id -> warning_count
        self._max_cycling_warnings = 3  # Nach 3 Warnings unterdrücken
        
        # Dynamische Energy-Sensor-Meldungen
        self._energy_warnings = {}  # Dict: entity_id -> warning_count
        self._max_energy_warnings = 3  # Nach 3 Warnings unterdrücken
        
        # Flag für Initialisierung - verhindert Flankenerkennung beim ersten Update
        self._initialization_complete = False

        # Fast polling for edge detection (HP_STATE / HP_OPERATING_STATE only)
        self._full_update_running = False  # True while _async_update_data holds Modbus
        self._unsub_fast_poll = None

        # Persist File I/O Optimierung
        self._persist_dirty = False  # Dirty-Flag für Änderungen
        self._persist_last_write = 0  # Timestamp des letzten Schreibens
        self._persist_debounce_seconds = 30  # Max 1x pro 30 Sekunden schreiben

        # self._load_offsets_and_persisted() ENTFERNT!

    async def _increment_thermal_energy_consumption(self, hp_idx, mode, energy_delta):
        """Increment thermal energy consumption for a specific mode and heat pump."""
        try:
            from .utils import increment_energy_consumption_counter
            hp_key = f"hp{hp_idx}"
            energy_offsets = self._energy_offsets.get(hp_key, {})
            name_prefix = normalize_name_prefix(self.entry.data.get("name", "")) or "eu08l"
            await increment_energy_consumption_counter(
                hass=self.hass,
                mode=mode,
                hp_index=hp_idx,
                energy_delta=energy_delta,
                name_prefix=name_prefix,
                use_legacy_modbus_names=self._use_legacy_names,
                energy_offsets=energy_offsets,
                sensor_type="thermal",
            )
        except Exception as ex:
            _LOGGER.error("Error incrementing thermal energy consumption for HP%s %s: %s", hp_idx, mode, ex)

    def _normalize_operating_states(self, states_dict):
        """Normalisiere last_operating_states (HP_OPERATING_STATE, Register 1003) - konvertiere alle Schlüssel zu Strings."""
        if not isinstance(states_dict, dict):
            return {}
        
        normalized = {}
        for key, value in states_dict.items():
            # Konvertiere Schlüssel zu String
            normalized[str(key)] = value
        
        return normalized
    
    def _normalize_states(self, states_dict):
        """Normalisiere last_states (HP_STATE, Register 1002) - konvertiere alle Schlüssel zu Strings.
        
        Diese Methode ist für HP_STATE Werte (z.B. START COMPRESSOR = 5) gedacht,
        die sich semantisch von HP_OPERATING_STATE Werten unterscheiden.
        """
        if not isinstance(states_dict, dict):
            return {}
        
        normalized = {}
        for key, value in states_dict.items():
            # Konvertiere Schlüssel zu String
            normalized[str(key)] = value
        
        return normalized

    async def _repair_and_load_persist_file(self):
        """Lade persistierte JSON-Datei, normalisiere und fülle fehlende Felder auf."""
        _REQUIRED_FIELDS = [
            "heating_cycles", "heating_energy", "last_operating_states",
            "energy_consumption", "last_energy_readings", "last_thermal_energy_readings",
            "energy_offsets", "sensor_ids", "thermal_sensor_ids", "energy_sensor_states",
        ]

        def _load_and_normalize():
            try:
                with open(self._persist_file) as f:
                    content = f.read().strip()

                if not content:
                    _LOGGER.warning("Persist file %s is empty, using defaults", self._persist_file)
                    return {}

                data = json.loads(content)

                # Normalisiere last_operating_states-Schlüssel zu Strings
                if isinstance(data.get("last_operating_states"), dict):
                    data["last_operating_states"] = {
                        str(k): v for k, v in data["last_operating_states"].items()
                    }

                # Fehlende Felder mit leeren Dicts aufüllen
                for field in _REQUIRED_FIELDS:
                    if field not in data:
                        data[field] = {}
                        _LOGGER.debug("Added missing field '%s' to loaded persist data", field)

                return data

            except json.JSONDecodeError as e:
                _LOGGER.error("Corrupted persist file %s: %s — backing up and starting fresh", self._persist_file, e)
                try:
                    backup_file = self._persist_file + ".backup"
                    with open(self._persist_file, "r") as src, open(backup_file, "w") as dst:
                        dst.write(src.read())
                    os.remove(self._persist_file)
                except Exception as backup_err:
                    _LOGGER.warning("Could not back up corrupted persist file: %s", backup_err)
                return {}

            except Exception as e:
                _LOGGER.error("Error reading persist file %s: %s", self._persist_file, e)
                return {}

        return await self.hass.async_add_executor_job(_load_and_normalize)

    async def _persist_counters(self, force: bool = False):
        """Persist counter data and state information to file using optimized I/O with debouncing."""
        import time

        # Prüfe Dirty-Flag - nur schreiben wenn sich etwas geändert hat
        if not self._persist_dirty:
            _LOGGER.debug("No changes to persist, skipping write")
            return

        current_time = time.time()

        # Debouncing: Max 1x pro 30 Sekunden schreiben (außer bei force=True beim Shutdown)
        if not force and current_time - self._persist_last_write < self._persist_debounce_seconds:
            _LOGGER.debug("Persist write debounced (last write %.1fs ago)",
                         current_time - self._persist_last_write)
            return
        
        # Stelle sicher, dass alle Schlüssel konsistent sind
        # Verwende separate Normalisierungsmethoden für semantisch unterschiedliche State-Typen
        normalized_operating_states = self._normalize_operating_states(
            getattr(self, "_last_operating_state", {})
        )
        normalized_states = self._normalize_states(
            getattr(self, "_last_state", {})
        )
        
        # Prüfe ob sensor_ids/thermal_sensor_ids in der Datei existieren, falls im Speicher leer
        sensor_ids_to_save = getattr(self, "_sensor_ids", {})
        thermal_sensor_ids_to_save = getattr(self, "_thermal_sensor_ids", {})
        if (not sensor_ids_to_save or not thermal_sensor_ids_to_save) and os.path.exists(self._persist_file):

            def _read_existing_sensor_ids():
                try:
                    with open(self._persist_file) as f:
                        existing_data = json.loads(f.read())
                        out_sensor = sensor_ids_to_save or existing_data.get("sensor_ids", {})
                        out_thermal = thermal_sensor_ids_to_save or existing_data.get("thermal_sensor_ids", {})
                        return out_sensor, out_thermal
                except Exception:
                    return sensor_ids_to_save, thermal_sensor_ids_to_save

            sensor_ids_to_save, thermal_sensor_ids_to_save = await self.hass.async_add_executor_job(_read_existing_sensor_ids)

        # Energy-Sensor-States aus Entities sammeln (electrical + thermal, alle Perioden)
        energy_sensor_states_to_save = self._collect_energy_sensor_states()

        data = {
            "version": 1,
            "heating_cycles": self._heating_cycles,
            "heating_energy": self._heating_energy,
            "last_operating_states": normalized_operating_states,
            "energy_last_operating_states": self._normalize_operating_states(
                getattr(self, "_energy_last_operating_state", {})
            ),
            "last_states": normalized_states,
            "energy_consumption": self._energy_consumption,
            "last_energy_readings": self._last_energy_reading,
            "last_thermal_energy_readings": getattr(self, "_last_thermal_energy_reading", {}),
            "energy_offsets": self._energy_offsets,
            "sensor_ids": sensor_ids_to_save,
            "thermal_sensor_ids": thermal_sensor_ids_to_save,
            "energy_sensor_states": energy_sensor_states_to_save,
        }

        def _write_data():
            os.makedirs(os.path.dirname(self._persist_file), exist_ok=True)
            with open(self._persist_file, "w") as f:
                json.dump(data, f, indent=2)  # Mit Indentation für bessere Lesbarkeit

        # Schreibe als Background-Task (non-blocking)
        try:
            await self.hass.async_add_executor_job(_write_data)
            self._persist_last_write = current_time
            self._persist_dirty = False  # Reset Dirty-Flag nach erfolgreichem Schreiben
            _LOGGER.debug("Persist file written successfully")
        except Exception as e:
            _LOGGER.error("Failed to write persist file: %s", e)
            # Dirty-Flag bleibt True für nächsten Versuch

    def mark_initialization_complete(self) -> None:
        """Markiere die Initialisierung als abgeschlossen - ermöglicht Flankenerkennung."""
        if not self._initialization_complete:
            self._initialization_complete = True
            _LOGGER.info("Coordinator-Initialisierung abgeschlossen - Flankenerkennung aktiviert")

    def _collect_energy_sensor_states(self):
        """Sammle State + Attribute aller Energy-Consumption-Entities für Persist (electrical + thermal)."""
        out = {}
        try:
            comp = self.hass.data.get("lambda_heat_pumps", {}).get(self.entry.entry_id, {})
            entities = comp.get("energy_entities", {})
            # energy_entities ist ueber unique_id geschluesselt (stabil, siehe
            # increment_energy_consumption_counter()); fuer die Persist-Datei wird aber
            # die reale entity_id gebraucht (get_energy_sensor_persisted_state() wird mit
            # self.entity_id aufgerufen) - die kommt direkt von der Entity-Instanz.
            for _unique_id, ent in entities.items():
                if not hasattr(ent, "_energy_value"):
                    continue
                entity_id = getattr(ent, "entity_id", None)
                if not entity_id:
                    continue
                state_val = ent.native_value
                if state_val is None:
                    continue
                energy_val = round(getattr(ent, "_energy_value", 0), 2)
                yesterday_val = round(getattr(ent, "_yesterday_value", 0), 2)
                prev_monthly_val = round(getattr(ent, "_previous_monthly_value", 0), 2)
                prev_yearly_val = round(getattr(ent, "_previous_yearly_value", 0), 2)
                # Konsistenz: Basis-Wert darf nicht größer als energy_value sein (Periodenwert wäre sonst negativ)
                if "_daily" in entity_id and yesterday_val > energy_val:
                    yesterday_val = energy_val
                if "_monthly" in entity_id and prev_monthly_val > energy_val:
                    prev_monthly_val = energy_val
                if "_yearly" in entity_id and prev_yearly_val > energy_val:
                    prev_yearly_val = energy_val
                applied_offset_val = round(getattr(ent, "_applied_offset", 0.0), 4)
                attrs = {
                    "energy_value": energy_val,
                    "yesterday_value": yesterday_val,
                    "previous_monthly_value": prev_monthly_val,
                    "previous_yearly_value": prev_yearly_val,
                    "applied_offset": applied_offset_val,
                }
                out[entity_id] = {"state": round(float(state_val), 2), "attributes": attrs}
        except Exception as e:
            _LOGGER.debug("Collect energy_sensor_states: %s", e)
        return out

    def get_energy_sensor_persisted_state(self, entity_id):
        """Liefert den aus cycle_energy_persist geladenen State für eine Entity (oder None)."""
        return self._energy_sensor_states.get(entity_id)

    def set_energy_persist_dirty(self) -> None:
        """Markiert Persist als geändert, damit Energy-States beim nächsten Schreibzyklus mit gespeichert werden."""
        self._persist_dirty = True

    async def _load_offsets_and_persisted(self):
        # Lade Offsets aus lambda_wp_config.yaml über das zentrale Config-System
        from .utils import load_lambda_config
        
        try:
            config = await load_lambda_config(self.hass)
            _LOGGER.info("Loaded config keys: %s", list(config.keys()))
            self._cycling_offsets = config.get("cycling_offsets", {})
            self._energy_offsets = config.get("energy_consumption_offsets", {})
            # Lade und validiere Energy Sensor Konfigurationen
            raw_energy_sensor_configs = config.get("energy_consumption_sensors", {})
            
            # Validiere externe Sensoren
            from .utils import validate_external_sensors
            self._energy_sensor_configs = validate_external_sensors(self.hass, raw_energy_sensor_configs)
            
            _LOGGER.info("Loaded energy sensor configs: %s", self._energy_sensor_configs)
            
            # Info-Message: Anzeige der verwendeten Quellsensoren für Verbrauchswerte
            if self._energy_sensor_configs:
                _LOGGER.info("=== ENERGY CONSUMPTION SENSORS ===")
                for hp_key, sensor_config in self._energy_sensor_configs.items():
                    sensor_id = sensor_config.get("sensor_entity_id")
                    _LOGGER.info("Energy consumption tracking for %s: using custom sensor '%s'", hp_key.upper(), sensor_id)
            else:
                _LOGGER.info("=== ENERGY CONSUMPTION SENSORS ===")
                _LOGGER.info("Energy consumption tracking: using default internal Modbus sensors")
                _LOGGER.info("(Configure custom sensors in lambda_wp_config.yaml if needed)")
        except Exception as e:
            _LOGGER.error("Error loading config: %s", e)
            # Fallback zu leeren Werten
            self._cycling_offsets = {}
            self._energy_offsets = {}
            self._energy_sensor_configs = {}

        # Lade persistierte Zählerstände (falls vorhanden) mit Reparatur-Funktion
        if os.path.exists(self._persist_file):
            data = await self._repair_and_load_persist_file()
        else:
            data = {}
        
        self._heating_cycles = data.get("heating_cycles", {})
        self._heating_energy = data.get("heating_energy", {})
        
        # Lade persistierte State-Informationen
        self._last_operating_state = data.get("last_operating_states", {})
        self._energy_last_operating_state = data.get("energy_last_operating_states", {})
        self._last_state = data.get("last_states", {})
        
        # Lade persistierte Energy Consumption Daten
        self._energy_consumption = data.get("energy_consumption", {})
        self._sensor_ids = data.get("sensor_ids", {})
        self._thermal_sensor_ids = data.get("thermal_sensor_ids", {})
        self._energy_sensor_states = data.get("energy_sensor_states", {})
        # Energy Offsets werden bereits aus der Config geladen

        # last_energy_reading: Bei Sensor-Wechsel sofort auf None setzen (bevor es verwendet wird),
        # damit kein falsches Delta (alter last + neuer Sensor) addiert wird – verhindert Sprung/Spike.
        loaded_last = data.get("last_energy_readings", {})
        corrected_last = dict(loaded_last)
        from .utils import detect_sensor_change, get_stored_sensor_id, get_stored_thermal_sensor_id
        all_hp_keys = set(self._energy_sensor_configs.keys()) | set(self._sensor_ids.keys()) | set(self._thermal_sensor_ids.keys())
        persist_data = {"sensor_ids": self._sensor_ids, "thermal_sensor_ids": self._thermal_sensor_ids}
        for hp_key in all_hp_keys:
            try:
                hp_idx = int(hp_key.replace("hp", ""))
            except (ValueError, AttributeError):
                continue
            current_sensor_id = None
            if hp_key in self._energy_sensor_configs:
                current_sensor_id = self._energy_sensor_configs[hp_key].get("sensor_entity_id")
            if not current_sensor_id:
                current_sensor_id = self._default_internal_energy_entity_id(
                    hp_idx, "electrical"
                )
            stored_sensor_id = get_stored_sensor_id(persist_data, hp_idx)
            if detect_sensor_change(stored_sensor_id, current_sensor_id):
                corrected_last[hp_key] = None
                self._energy_first_value_seen[hp_key] = False
                _LOGGER.info(
                    "SENSOR-CHANGE-DETECTION: %s last_energy_reading sofort auf None gesetzt (Sensor-Wechsel, verhindert Delta-Sprung)",
                    hp_key,
                )
        self._last_energy_reading = corrected_last

        # Thermik: last_thermal_energy_reading bei Sensor-Wechsel auf None setzen
        loaded_thermal_last = data.get("last_thermal_energy_readings", {})
        corrected_thermal_last = dict(loaded_thermal_last)
        for hp_key in all_hp_keys:
            try:
                hp_idx = int(hp_key.replace("hp", ""))
            except (ValueError, AttributeError):
                continue
            current_thermal_id = None
            if hp_key in self._energy_sensor_configs:
                current_thermal_id = self._energy_sensor_configs[hp_key].get("thermal_sensor_entity_id")
            if not current_thermal_id:
                current_thermal_id = self._default_internal_energy_entity_id(
                    hp_idx, "thermal"
                )
            stored_thermal_id = get_stored_thermal_sensor_id(persist_data, hp_idx)
            if detect_sensor_change(stored_thermal_id, current_thermal_id):
                corrected_thermal_last[hp_key] = None
                self._thermal_energy_first_value_seen[hp_key] = False
                _LOGGER.info(
                    "SENSOR-CHANGE-DETECTION: %s last_thermal_energy_reading auf None (Thermik-Sensor-Wechsel)",
                    hp_key,
                )
        self._last_thermal_energy_reading = corrected_thermal_last

        _LOGGER.info("SENSOR-CHANGE-DETECTION: Geladene sensor_ids: %s", self._sensor_ids)
        
        _LOGGER.info(
            f"Restored last_operating_state: {self._last_operating_state}"
        )

        # Sensor-Wechsel-Erkennung für Energy Consumption Sensoren (NACH dem Laden der persistierten Daten)
        # Führe die Erkennung bei jedem Start aus, um Sensor-Wechsel zu erkennen
        _LOGGER.info("SENSOR-CHANGE-DETECTION: Starte Sensor-Wechsel-Erkennung für %s konfigurierte Sensoren", len(self._energy_sensor_configs))
        await self._detect_and_handle_sensor_changes()

    async def _detect_and_handle_sensor_changes(self):
        """Erkenne Sensor-Wechsel und behandle sie entsprechend."""
        _LOGGER.info("SENSOR-CHANGE-DETECTION: Starte Sensor-Wechsel-Erkennung")
        
        try:
            # Arbeite auf Kopien, um atomaren Tausch am Ende zu ermöglichen
            local_sensor_ids = dict(self._sensor_ids)
            local_thermal_sensor_ids = dict(self._thermal_sensor_ids)
            persist_data = {"sensor_ids": local_sensor_ids, "thermal_sensor_ids": local_thermal_sensor_ids}
            
            # Prüfe alle Wärmepumpen, die in _sensor_ids gespeichert sind (auch wenn keine Custom-Sensoren konfiguriert sind)
            all_hp_keys = set()
            
            # Füge Custom-Sensoren hinzu
            for hp_key in self._energy_sensor_configs.keys():
                all_hp_keys.add(hp_key)
            
            # Füge alle Wärmepumpen aus _sensor_ids und _thermal_sensor_ids hinzu
            for hp_key in self._sensor_ids.keys():
                all_hp_keys.add(hp_key)
            for hp_key in self._thermal_sensor_ids.keys():
                all_hp_keys.add(hp_key)
            
            _LOGGER.info("SENSOR-CHANGE-DETECTION: Prüfe %s Wärmepumpen: %s", len(all_hp_keys), sorted(all_hp_keys))
            
            for hp_key in all_hp_keys:
                # Extrahiere hp_idx aus hp_key (z.B. "hp1" -> 1)
                try:
                    hp_idx = int(hp_key.replace("hp", ""))
                except (ValueError, AttributeError):
                    _LOGGER.warning("SENSOR-CHANGE-DETECTION: Ungültiger hp_key: %s", hp_key)
                    continue
                
                # Bestimme aktuellen Sensor (Custom oder Default)
                current_sensor_id = None
                
                # Prüfe zuerst Custom-Sensor
                if hp_key in self._energy_sensor_configs:
                    current_sensor_id = self._energy_sensor_configs[hp_key].get("sensor_entity_id")
                    _LOGGER.info("SENSOR-CHANGE-DETECTION: %s - Custom-Sensor: %s", hp_key, current_sensor_id)
                
                # Falls kein Custom-Sensor, verwende Default-Sensor (lowercase wie entity_id)
                if not current_sensor_id:
                    current_sensor_id = self._default_internal_energy_entity_id(
                        hp_idx, "electrical"
                    )
                    _LOGGER.info("SENSOR-CHANGE-DETECTION: %s - Default-Sensor: %s", hp_key, current_sensor_id)
                
                _LOGGER.info("SENSOR-CHANGE-DETECTION: Prüfe %s - aktueller Sensor: %s", hp_key, current_sensor_id)
                
                # Hole gespeicherte Sensor-ID
                stored_sensor_id = get_stored_sensor_id(persist_data, hp_idx)
                
                # Prüfe auf Sensor-Wechsel
                if detect_sensor_change(stored_sensor_id, current_sensor_id):
                    _LOGGER.info("SENSOR-CHANGE-DETECTION: Sensor-Wechsel erkannt für %s: %s -> %s", hp_key, stored_sensor_id, current_sensor_id)
                    await self._handle_sensor_change(hp_idx, current_sensor_id)
                
                # Speichere neue Sensor-ID in lokale Kopie
                store_sensor_id(persist_data, hp_idx, current_sensor_id)
                _LOGGER.info("SENSOR-CHANGE-DETECTION: Sensor-ID für %s aktualisiert: %s", hp_key, current_sensor_id)

            # Thermik-Sensor-Wechsel prüfen (analog zu elektrisch)
            for hp_key in all_hp_keys:
                try:
                    hp_idx = int(hp_key.replace("hp", ""))
                except (ValueError, AttributeError):
                    continue
                current_thermal_id = None
                if hp_key in self._energy_sensor_configs:
                    current_thermal_id = self._energy_sensor_configs[hp_key].get("thermal_sensor_entity_id")
                if not current_thermal_id:
                    current_thermal_id = self._default_internal_energy_entity_id(
                        hp_idx, "thermal"
                    )
                stored_thermal_id = get_stored_thermal_sensor_id(persist_data, hp_idx)
                if detect_sensor_change(stored_thermal_id, current_thermal_id):
                    _LOGGER.info(
                        "SENSOR-CHANGE-DETECTION: Thermik-Sensor-Wechsel für %s: %s -> %s",
                        hp_key, stored_thermal_id, current_thermal_id,
                    )
                    await self._handle_thermal_sensor_change(hp_idx, current_thermal_id)
                store_thermal_sensor_id(persist_data, hp_idx, current_thermal_id)

            # Atomar tauschen — kein yield zwischen diesen beiden Zeilen
            self._sensor_ids = persist_data["sensor_ids"]
            self._thermal_sensor_ids = persist_data["thermal_sensor_ids"]

            # Speichere alle Änderungen in der JSON-Datei
            if self._sensor_ids or self._thermal_sensor_ids:
                _LOGGER.info(
                    "SENSOR-CHANGE-DETECTION: Speichere sensor_ids + thermal_sensor_ids in JSON"
                )
                self._persist_dirty = True
                await self._persist_counters()
                _LOGGER.info("SENSOR-CHANGE-DETECTION: sensor_ids erfolgreich gespeichert")
            
            _LOGGER.info("SENSOR-CHANGE-DETECTION: Sensor-Wechsel-Erkennung abgeschlossen")
            
        except Exception as e:
            _LOGGER.error("SENSOR-CHANGE-DETECTION: Fehler bei Sensor-Wechsel-Erkennung: %s", e)
            import traceback
            _LOGGER.error("SENSOR-CHANGE-DETECTION: Traceback: %s", traceback.format_exc())

    async def _handle_sensor_change(self, hp_idx: int, new_sensor_id: str):
        """Behandle Sensor-Wechsel mit intelligenter DB-Wert-Nutzung."""
        _LOGGER.info("SENSOR-CHANGE: === SENSOR-WECHSEL ERKANNT HP%s ===", hp_idx)
        _LOGGER.info("SENSOR-CHANGE: Neuer Sensor: %s", new_sensor_id)
        
        hp_key = f"hp{hp_idx}"
        
        # Prüfe ob es ein Default-Sensor ist (interner Modbus-Sensor)
        default_sensor_id = self._default_internal_energy_entity_id(hp_idx, "electrical")

        is_default_sensor = (new_sensor_id == default_sensor_id)
        _LOGGER.info("SENSOR-CHANGE: Erwarteter Default-Sensor: %s", default_sensor_id)
        _LOGGER.info("SENSOR-CHANGE: Ist Default-Sensor: %s", is_default_sensor)
        
        if is_default_sensor:
            # SCHRITT 3: Wechsel zu Default-Sensor (interner Modbus)
            _LOGGER.info("SENSOR-CHANGE: → SCHRITT 3: Wechsel zu Default-Sensor (interner Modbus)")
            _LOGGER.info("SENSOR-CHANGE: → Lese DB-Wert vom Default-Sensor...")
            
            # Lese letzten DB-Wert vom Default-Sensor
            db_state = self.hass.states.get(new_sensor_id)
            _LOGGER.info("SENSOR-CHANGE: → DB-State: %s", (db_state.state if db_state else 'None'))
            
            if db_state and db_state.state not in ("unknown", "unavailable", "None"):
                try:
                    db_value = float(db_state.state)
                    _LOGGER.info("SENSOR-CHANGE: → DB-Wert konvertiert: %.2f kWh", db_value)
                    
                    if db_value > 0:
                        _LOGGER.info("SENSOR-CHANGE: → DB-Wert > 0: %.2f kWh", db_value)
                        _LOGGER.info("SENSOR-CHANGE: → Setze als Referenz für sofortige Delta-Berechnung")
                        _LOGGER.info("SENSOR-CHANGE: → Nächster Messwert wird mit diesem DB-Wert verglichen")
                        
                        # Setze DB-Wert als last_energy
                        self._last_energy_reading[hp_key] = db_value
                        self._energy_first_value_seen[hp_key] = True
                        self._persist_dirty = True
                        await self._persist_counters()
                        _LOGGER.info("SENSOR-CHANGE: → Referenzwert gesetzt, warte auf ersten Messwert")
                        _LOGGER.info("SENSOR-CHANGE: === SENSOR-WECHSEL ABGESCHLOSSEN: DB-REFERENZ GESETZT ===")
                        return
                        
                    else:
                        # DB-Wert ist 0
                        _LOGGER.info("SENSOR-CHANGE: → DB-Wert = 0, keine Historie verfügbar")
                        _LOGGER.info("SENSOR-CHANGE: → Starte Zero-Value Protection")
                        
                except (ValueError, TypeError) as e:
                    _LOGGER.warning("SENSOR-CHANGE: → Fehler beim Konvertieren des DB-Werts: %s", e)
                    _LOGGER.info("SENSOR-CHANGE: → Starte Zero-Value Protection")
            else:
                _LOGGER.info("SENSOR-CHANGE: → Kein DB-Wert verfügbar (State: %s)", (db_state.state if db_state else 'None'))
                _LOGGER.info("SENSOR-CHANGE: → Starte Zero-Value Protection")
        
        else:
            # SCHRITT 4: Wechsel zu externem/Custom Sensor
            _LOGGER.info("SENSOR-CHANGE: → SCHRITT 4: Wechsel zu externem/Custom Sensor")
            _LOGGER.info("SENSOR-CHANGE: → Keine DB-Historie verfügbar für externe Sensoren")
            _LOGGER.info("SENSOR-CHANGE: → Starte Zero-Value Protection")
        
        # Fallback: Zero-Value Protection
        _LOGGER.info("SENSOR-CHANGE: → AKTIVIERE ZERO-VALUE PROTECTION")
        _LOGGER.info("SENSOR-CHANGE: → Warte auf 2 aufeinanderfolgende Werte > 0")
        _LOGGER.info("SENSOR-CHANGE: → Erster Wert wird gespeichert, aber kein Delta berechnet")
        _LOGGER.info("SENSOR-CHANGE: → Ab zweitem Wert wird Delta berechnet")
        
        self._last_energy_reading[hp_key] = None
        self._energy_first_value_seen[hp_key] = False
        self._persist_dirty = True
        await self._persist_counters()
        _LOGGER.info("SENSOR-CHANGE: === SENSOR-WECHSEL ABGESCHLOSSEN: ZERO-VALUE PROTECTION AKTIV ===")

    async def _handle_thermal_sensor_change(self, hp_idx: int, new_sensor_id: str):
        """Behandle Thermik-Sensor-Wechsel (analog zu _handle_sensor_change)."""
        _LOGGER.info("SENSOR-CHANGE: === THERMIK-SENSOR-WECHSEL HP%s === Neuer Sensor: %s", hp_idx, new_sensor_id)
        hp_key = f"hp{hp_idx}"
        default_thermal_id = self._default_internal_energy_entity_id(hp_idx, "thermal")
        is_default = new_sensor_id == default_thermal_id
        if is_default:
            db_state = self.hass.states.get(new_sensor_id)
            if db_state and db_state.state not in ("unknown", "unavailable", "None"):
                try:
                    db_value = float(db_state.state)
                    if db_value > 0:
                        self._last_thermal_energy_reading[hp_key] = db_value
                        self._thermal_energy_first_value_seen[hp_key] = True
                        self._persist_dirty = True
                        await self._persist_counters()
                        _LOGGER.info("SENSOR-CHANGE: Thermik-Referenzwert gesetzt HP%s: %.2f kWh", hp_idx, db_value)
                        return
                except (ValueError, TypeError):
                    pass
        self._last_thermal_energy_reading[hp_key] = None
        self._thermal_energy_first_value_seen[hp_key] = False
        self._persist_dirty = True
        await self._persist_counters()
        _LOGGER.info("SENSOR-CHANGE: Thermik Zero-Value Protection aktiv HP%s", hp_idx)


    async def async_init(self) -> None:
        """Async initialization (inkl. Modbus-Connect für Auto-Detection)."""
        _LOGGER.debug("Initializing Lambda coordinator")
        _LOGGER.debug("Config directory: %s", self._config_dir)
        _LOGGER.debug("Config path: %s", self._config_path)

        try:
            await self._ensure_config_dir()
            _LOGGER.debug("Config directory ensured")

            self.disabled_registers = await load_disabled_registers(self.hass)
            _LOGGER.debug("Loaded disabled registers: %s", self.disabled_registers)

            # Lade sensor_overrides direkt beim Init
            self.sensor_overrides = await self._load_sensor_overrides()
            _LOGGER.debug("Loaded sensor name overrides: %s", self.sensor_overrides)

            # Initialize HA started flag
            self._ha_started = False

            # Register event listener for Home Assistant started
            self.hass.bus.async_listen_once(
                "homeassistant_started", self._on_ha_started
            )

            if not self.disabled_registers:
                _LOGGER.debug(
                    "No disabled registers configured - this is normal if you "
                    "haven't disabled any registers"
                )
            
            # Lade Offsets und persistierte Daten (immer, unabhängig von disabled registers)
            await self._load_offsets_and_persisted()

            # Modbus-Connect für Auto-Detection (wird im Produktivbetrieb ohnehin benötigt)
            await self._connect()

            # Initialize Entity Registry monitoring
            # Entity-based polling control now handled by entity lifecycle methods

        except Exception as e:
            _LOGGER.error("Failed to initialize coordinator: %s", str(e))
            self.disabled_registers = set()
            self.sensor_overrides = {}
            raise

    async def _ensure_config_dir(self):
        """Ensure config directory exists."""
        try:

            def _create_dirs():
                os.makedirs(self._config_dir, exist_ok=True)
                os.makedirs(self._config_path, exist_ok=True)
                _LOGGER.debug(
                    "Created directories: %s and %s",
                    self._config_dir,
                    self._config_path,
                )

            await self.hass.async_add_executor_job(_create_dirs)
        except Exception as e:
            _LOGGER.error("Failed to create config directories: %s", str(e))
            raise

    def is_register_disabled(self, address: int) -> bool:
        """Check if a register is disabled."""
        if not hasattr(self, "disabled_registers"):
            _LOGGER.error("disabled_registers not initialized")
            return False

        # Debug: Ausgabe der Typen und Inhalte
        _LOGGER.debug(
            "Check if address %r (type: %s) is in disabled_registers: %r (types: %r)",
            address,
            type(address),
            self.disabled_registers,
            {type(x) for x in self.disabled_registers},
        )

        is_disabled = is_register_disabled(address, self.disabled_registers)
        if is_disabled:
            _LOGGER.debug(
                "Register %d is disabled (in set: %s)",
                address,
                self.disabled_registers,
            )
        else:
            _LOGGER.debug(
                "Register %d is not disabled (checked against set: %s)",
                address,
                self.disabled_registers,
            )
        return is_disabled

    async def _connect(self) -> None:
        """Open the Modbus link and take a handle for our unit on it."""
        if self.connection is not None and self.connection.connected:
            _LOGGER.debug("MODBUS CONNECT: Already connected to %s:%s", self.host, self.port)
            return

        _LOGGER.info(
            "MODBUS CONNECT: Connecting to %s:%s (coordinator_id=%s)",
            self.host, self.port, id(self),
        )
        try:
            self.connection = await connect_tcp(self.host, port=self.port)
        except ModbusError as err:
            self.connection = None
            self.unit = None
            self.device = None
            _LOGGER.warning(
                "MODBUS CONNECT: Failed to connect to %s:%s: %s", self.host, self.port, err
            )
            raise UpdateFailed(f"Connection failed: {err}") from err

        self.unit = self.connection.for_unit(self.slave_id)
        _LOGGER.info("MODBUS CONNECT: Connected to %s:%s", self.host, self.port)

    def _build_device(self) -> LambdaHeatPump:
        """Model the controller with the modules this entry is configured for.

        Built on the first update rather than at connect time: the module counts
        can still be corrected by auto-detection, and the 32-bit word order is
        read from lambda_wp_config.yaml after the coordinator is constructed.
        """
        device = LambdaHeatPump(
            self.unit,
            num_hps=self.entry.data.get("num_hps", 1),
            num_boil=self.entry.data.get("num_boil", 1),
            num_buff=self.entry.data.get("num_buff", 0),
            num_sol=self.entry.data.get("num_sol", 0),
            num_hc=self.entry.data.get("num_hc", 1),
            word_order="little" if self._int32_register_order == "low_first" else "big",
        )
        _LOGGER.debug(
            "Modelled controller: %d component(s), word order %s",
            len(device.components), self._int32_register_order,
        )
        return device

    def component_for(self, attribute: str, index: int | None = None):
        """The modelled sub-system an entity reads from.

        `attribute` is the name it has on LambdaHeatPump — `ambient`,
        `e_manager`, or one of the module lists (`heat_pumps`, `boilers`, …), in
        which case `index` is the 1-based module number. Returns None before the
        first successful update, when there is no model yet.
        """
        if self.device is None:
            return None
        target = getattr(self.device, attribute)
        if index is None:
            return target
        return target[index - 1] if 1 <= index <= len(target) else None

    def _collect_values(self, fw_version: int) -> dict:
        """Read the modelled device's values into the flat dict the entities use.

        Keys are what they have always been — `ambient_temperature`,
        `hp1_flow_line_temperature`, `boil2_target_high_temperature` — and the
        register templates stay the source of truth for which of them exist, so
        firmware filtering and sensor name overrides still apply.
        """
        data = {}

        def store(key, component, field):
            value = getattr(component, field, None)
            if value is None:
                return  # never read, or the controller reported no value
            data[self.sensor_overrides.get(key, key)] = value

        for key, sensor_info in get_compatible_sensors(SENSOR_TYPES, fw_version).items():
            if self.is_register_disabled(sensor_info["address"]):
                continue
            prefix = next(p for p in GENERAL_PREFIXES if key.startswith(p))
            component = getattr(self.device, GENERAL_PREFIXES[prefix])
            store(key, component, key.removeprefix(prefix))

        for module, (templates, attribute) in MODULE_TEMPLATES.items():
            compatible = get_compatible_sensors(templates, fw_version)
            for index, component in enumerate(getattr(self.device, attribute), 1):
                for field in compatible:
                    store(f"{module}{index}_{field}", component, field)

        return data

    def _cycling_entities_ready(self) -> bool:
        """Check whether cycling counter entities are registered and ready."""
        try:
            return (
                "lambda_heat_pumps" in self.hass.data
                and self.entry.entry_id in self.hass.data["lambda_heat_pumps"]
                and "cycling_entities" in self.hass.data["lambda_heat_pumps"][self.entry.entry_id]
            )
        except Exception:
            return False

    async def _run_cycling_edge_detection(self, data: dict) -> None:
        """Run edge detection on HP_OPERATING_STATE (reg 1003) and compressor_unit_rating (reg 1010).

        Called exclusively by _async_fast_update. Increments cycling counters on
        rising-edge transitions. Updates _last_operating_state and _last_compressor_rating.
        """
        num_hps = self.entry.data.get("num_hps", 1)
        MODES = {
            "heating": 1,
            "hot_water": 2,
            "cooling": 3,
            "defrost": 5,
        }

        # --- HP_OPERATING_STATE edge detection ---
        for hp_idx in range(1, num_hps + 1):
            op_state_val = data.get(f"hp{hp_idx}_operating_state")
            if op_state_val is None:
                continue

            last_op_state = self._last_operating_state.get(str(hp_idx), "UNBEKANNT")

            if last_op_state == "UNBEKANNT":
                _LOGGER.info(
                    "Fast poll: init _last_operating_state HP%d = %s", hp_idx, op_state_val
                )
            elif last_op_state != op_state_val:
                _LOGGER.debug(
                    "Fast poll: HP%d operating_state %s -> %s", hp_idx, last_op_state, op_state_val
                )

            for mode, mode_val in MODES.items():
                cycling_key = f"{mode}_cycles"
                if not hasattr(self, cycling_key):
                    setattr(self, cycling_key, {})
                cycles = getattr(self, cycling_key)

                _LOGGER.debug(
                    "FAST EDGE HP%s: init=%s last=%s mode_val=%s cur=%s",
                    hp_idx, self._initialization_complete, last_op_state, mode_val, op_state_val,
                )

                if (
                    self._initialization_complete
                    and last_op_state != "UNBEKANNT"
                    and last_op_state != mode_val
                    and op_state_val == mode_val
                ):
                    _LOGGER.info(
                        "Edge detected: HP%d operating state → %s (was %s)",
                        hp_idx, mode, last_op_state,
                    )
                    if self._cycling_entities_ready():
                        await increment_cycling_counter(
                            self.hass,
                            mode=mode,
                            hp_index=hp_idx,
                            name_prefix=self.entry.data.get("name", "eu08l"),
                            use_legacy_modbus_names=self._use_legacy_names,
                        )
                        old_count = cycles.get(hp_idx, 0)
                        if not isinstance(old_count, (int, float)):
                            old_count = 0
                        new_count = old_count + 1
                        cycles[hp_idx] = new_count
                        _LOGGER.info(
                            "🔄 FAST EDGE: HP%d %s → %s | %s: %d → %d",
                            hp_idx, last_op_state, op_state_val, mode, old_count, new_count,
                        )
                    else:
                        _LOGGER.debug(
                            "Fast poll: cycling entities not ready, skipping HP%d %s", hp_idx, mode
                        )
                elif not self._initialization_complete:
                    _LOGGER.debug(
                        "Fast poll: HP%d %s edge suppressed during init", hp_idx, mode
                    )

            self._last_operating_state[str(hp_idx)] = op_state_val

        # --- compressor_unit_rating edge detection (0 → >0 = compressor start) ---
        for hp_idx in range(1, num_hps + 1):
            rating_val = data.get(f"hp{hp_idx}_compressor_unit_rating")
            if rating_val is None:
                continue

            last_rating = self._last_compressor_rating.get(str(hp_idx), "UNBEKANNT")

            if last_rating == "UNBEKANNT":
                _LOGGER.info(
                    "Fast poll: init _last_compressor_rating HP%d = %s", hp_idx, rating_val
                )
            elif last_rating != rating_val:
                _LOGGER.debug(
                    "Fast poll: HP%d compressor_unit_rating %s -> %s", hp_idx, last_rating, rating_val
                )

            mode = "compressor_start"
            cycling_key = f"{mode}_cycles"
            if not hasattr(self, cycling_key):
                setattr(self, cycling_key, {})
            cycles = getattr(self, cycling_key)

            if (
                self._initialization_complete
                and last_rating != "UNBEKANNT"
                and last_rating == 0
                and rating_val != 0
            ):
                _LOGGER.info(
                    "Edge detected: HP%d compressor started (rating 0 → %s)",
                    hp_idx, rating_val,
                )
                if self._cycling_entities_ready():
                    await increment_cycling_counter(
                        self.hass,
                        mode=mode,
                        hp_index=hp_idx,
                        name_prefix=self.entry.data.get("name", "eu08l"),
                        use_legacy_modbus_names=self._use_legacy_names,
                    )
                    old_count = cycles.get(hp_idx, 0)
                    if not isinstance(old_count, (int, float)):
                        old_count = 0
                    new_count = old_count + 1
                    cycles[hp_idx] = new_count
                    _LOGGER.info(
                        "🔄 FAST EDGE compressor_unit_rating: HP%d 0 → %s | %s: %d → %d",
                        hp_idx, rating_val, mode, old_count, new_count,
                    )
                else:
                    _LOGGER.debug(
                        "Fast poll: cycling entities not ready, skipping HP%d %s (compressor_unit_rating)", hp_idx, mode
                    )
            elif not self._initialization_complete:
                _LOGGER.debug(
                    "Fast poll: HP%d %s (compressor_unit_rating) edge suppressed during init", hp_idx, mode
                )

            self._last_compressor_rating[str(hp_idx)] = rating_val

        self._persist_dirty = True

    async def _async_fast_update(self, now) -> None:
        """Fast poll: read HP_OPERATING_STATE (1003) and compressor_unit_rating (1010) for edge detection.

        Runs on fast_update_interval (default 2s). Serialized with the full update
        via _modbus_lock. Skips this cycle if the lock is already held.
        """
        if not self._initialization_complete or self.hass.is_stopping or self.unit is None:
            return

        if self._full_update_running:
            _LOGGER.debug("Fast poll skipped: full update in progress")
            return

        try:
            num_hps = self.entry.data.get("num_hps", 1)
            data = {}
            for hp_idx in range(1, num_hps + 1):
                base_addr = base_address("hp", hp_idx)
                # Two raw registers, straight off the unit: operating state (3)
                # and compressor unit rating (10). Edge detection only compares
                # them to themselves, so they are left unscaled.
                data[f"hp{hp_idx}_operating_state"] = (
                    await self.unit.read_holding_registers(base_addr + 3, 1)
                )[0]
                data[f"hp{hp_idx}_compressor_unit_rating"] = (
                    await self.unit.read_holding_registers(base_addr + 10, 1)
                )[0]

            _LOGGER.debug(
                "Fast poll: read %d HP(s) — %s",
                num_hps,
                ", ".join(
                    f"HP{i} op_state={data.get(f'hp{i}_operating_state', 'n/a')} comp_rating={data.get(f'hp{i}_compressor_unit_rating', 'n/a')}"
                    for i in range(1, num_hps + 1)
                ),
            )
            await self._run_cycling_edge_detection(data)

        except Exception as ex:
            _LOGGER.debug("Fast poll error (non-fatal): %s", ex)

    async def _async_update_data(self) -> dict:
        """Fetch data from Lambda device."""
        self._full_update_running = True
        try:
            _LOGGER.debug("PRODUCTION: Starting data update (coordinator_id=%s)", id(self))
            # Check if Home Assistant is shutting down
            if self.hass.is_stopping:
                _LOGGER.debug("Home Assistant is stopping, skipping data update")
                return self.data

            await self._connect()
            if self.device is None:
                self.device = self._build_device()

            # One pooled set of block reads for the whole controller. The plan
            # comes from the register model and is cached after the first update.
            await self.device.async_update()

            fw_version = get_firmware_version_int(self.entry)
            data = self._collect_values(fw_version)
            _LOGGER.debug("Read %d values from the controller", len(data))

            update_interval_seconds = self.entry.options.get("update_interval", DEFAULT_UPDATE_INTERVAL)
            interval = update_interval_seconds / 3600.0  # Intervall in Stunden
            
            # Debug: Start data update
            _LOGGER.debug("Starting _async_update_data")
            num_hps = self.entry.data.get("num_hps", 1)
            # Generische Flankenerkennung für alle relevanten Modi
            MODES = {
                "heating": 1,  # CH
                "hot_water": 2,  # DHW
                "cooling": 3,  # CC
                "defrost": 5,  # DEFROST
            }
            # HP_STATE Modi (separat, da auf HP_STATE Register basierend)
            HP_STATE_MODES = {
                "compressor_start": 2,  # RESTART-BLOCK
            }
            # Initialisiere _last_operating_state nur wenn nicht bereits aus Persistierung geladen
            if not hasattr(self, "_last_operating_state"):
                self._last_operating_state = {}
            # Initialisiere _last_state nur wenn nicht bereits aus Persistierung geladen
            if not hasattr(self, "_last_state"):
                self._last_state = {}

            # Dummy-Keys für Template-Sensoren einfügen
            # Erzeuge alle möglichen Template-Sensor-IDs
            num_hps = self.entry.data.get("num_hps", 1)
            num_boil = self.entry.data.get("num_boil", 1)
            num_buff = self.entry.data.get("num_buff", 0)
            num_sol = self.entry.data.get("num_sol", 0)
            num_hc = self.entry.data.get("num_hc", 1)
            DEVICE_COUNTS = {
                "hp": num_hps,
                "boil": num_boil,
                "buff": num_buff,
                "sol": num_sol,
                "hc": num_hc,
            }
            for device_type, count in DEVICE_COUNTS.items():
                for idx in range(1, count + 1):
                    device_prefix = f"{device_type}{idx}"
                    for sensor_id, sensor_info in CALCULATED_SENSOR_TEMPLATES.items():
                        if sensor_info.get("device_type") == device_type:
                            key = f"{device_prefix}_{sensor_id}"
                            # Setze einen sich ändernden Wert, z.B. Zeitstempel
                            data[key] = time.time()

            # Update room temperature and PV surplus only after Home Assistant
            # has started. This prevents timing issues with template sensors
            if hasattr(self, "_ha_started") and self._ha_started:
                # Note: Writing operations moved to services.py
                pass

            # Energieintegration für aktiven Modus (Cycling-Flankenerkennung läuft via _async_fast_update)
            for hp_idx in range(1, num_hps + 1):
                op_state_val = data.get(f"hp{hp_idx}_operating_state")
                if op_state_val is None:
                    continue

                for mode, mode_val in MODES.items():
                    energy_key = f"{mode}_energy"
                    if not hasattr(self, energy_key):
                        setattr(self, energy_key, {})
                    energy = getattr(self, energy_key)

                    if hp_idx not in energy:
                        energy[hp_idx] = 0.0
                    elif isinstance(energy[hp_idx], dict):
                        _LOGGER.warning("energy[%s] is a dict, converting to 0.0: %s", hp_idx, energy[hp_idx])
                        energy[hp_idx] = 0.0

                    power_info = HP_SENSOR_TEMPLATES.get("actual_heating_capacity")
                    if power_info:
                        power_val = data.get(f"hp{hp_idx}_actual_heating_capacity", 0.0)
                        if op_state_val == mode_val:
                            if not isinstance(energy[hp_idx], (int, float)):
                                energy[hp_idx] = 0.0
                            energy[hp_idx] = energy[hp_idx] + (power_val * interval)

            await self._persist_counters()
            
            # Setze Dirty-Flag wenn sich Werte geändert haben
            self._persist_dirty = True

            # Energy Consumption Tracking - NACH dem Lesen der Register
            _LOGGER.debug("DEBUG-001: Starting energy consumption tracking")
            await self._track_energy_consumption(data)
            _LOGGER.debug("DEBUG-002: Energy consumption tracking completed")

            _LOGGER.debug("PRODUCTION: Data update completed successfully (coordinator_id=%s)", id(self))
            return data

        except Exception as ex:
            _LOGGER.error("Error updating data: %s", ex)
            # The connection does not reconnect itself, so drop it and let the
            # next update open a fresh one — along with the device model, whose
            # read plan is tied to the unit it was built on.
            await self._close_connection()
            raise UpdateFailed(f"Error fetching Lambda data: {ex}")
        finally:
            self._full_update_running = False

    async def async_read_registers(self, address: int, count: int = 1) -> list[int]:
        """Read raw register words. Raises ModbusError if the read fails."""
        if self.unit is None:
            raise UpdateFailed("Not connected to the controller")
        return await self.unit.read_holding_registers(address, count)

    async def async_write_registers(self, address: int, values: list[int]) -> None:
        """Write raw register words. Raises ModbusError if the write fails.

        Entities that own a modelled field should prefer the field's own write
        (`coordinator.device.boilers[0].write("target_high_temperature", 52.5)`),
        which reverses the scaling for them. This is for the callers that still
        work in raw registers.
        """
        if self.unit is None:
            raise UpdateFailed("Not connected to the controller")
        if len(values) == 1:
            await self.unit.write_register(address, values[0])
        else:
            await self.unit.write_registers(address, values)

    async def _close_connection(self) -> None:
        """Close the Modbus link and forget the model built on it."""
        if self.connection is not None:
            try:
                await self.connection.close()
            except Exception as err:
                _LOGGER.debug("Error closing Modbus connection: %s", err)
        self.connection = None
        self.unit = None
        self.device = None

    def _is_energy_unit(self, unit: str) -> bool:
        """Check if unit is a valid energy unit."""
        if not unit:
            return True  # Leer ist OK (kWh)
        
        unit_lower = unit.lower().strip()
        valid_units = ["wh", "wattstunden", "kwh", "kilowattstunden", "mwh", "megawattstunden"]
        return unit_lower in valid_units

    def _convert_energy_to_kwh_cached(self, value: float, unit: str) -> float:
        """Optimized energy conversion using cached unit."""
        if not unit:  # Keine Einheit = kWh (Standard)
            return value
        
        if unit == "kWh":
            return value
        elif unit == "Wh":
            return value / 1000.0
        elif unit == "MWh":
            return value * 1000.0
        else:
            # Sollte nie erreicht werden, da ungültige Einheiten abgefangen werden
            _LOGGER.error("Unexpected unit '%s' in conversion function", unit)
            return value

    async def _track_energy_consumption(self, data):
        """Track energy consumption by operating mode."""
        _LOGGER.debug("DEBUG-004: Entering _track_energy_consumption")
        try:
            # Get number of heat pumps from config entry
            num_hps = self.entry.data.get("num_hps", 1)
            _LOGGER.debug("DEBUG-005: Number of heat pumps: %s", num_hps)
            
            # Get current operating states for all heat pumps
            current_states = {}
            for hp_idx in range(1, num_hps + 1):
                state_key = f"hp{hp_idx}_operating_state"
                _LOGGER.debug("DEBUG-006A: Available keys in data: %s", list(data.keys()))
                _LOGGER.debug("DEBUG-006B: Looking for key: %s", state_key)
                if state_key in data:
                    current_states[hp_idx] = data[state_key]
                    _LOGGER.debug("DEBUG-006C: Found %s = %s", state_key, data[state_key])
                else:
                    current_states[hp_idx] = 0  # Default to 0 if not available
                    _LOGGER.debug("DEBUG-006D: Key %s not found, using default 0", state_key)
                _LOGGER.debug("DEBUG-006: HP%s operating state: %s", hp_idx, current_states[hp_idx])

            # Track energy consumption for each heat pump
            for hp_idx in range(1, num_hps + 1):
                _LOGGER.debug("DEBUG-007: Tracking energy consumption for HP%s", hp_idx)
                await self._track_hp_energy_consumption(hp_idx, current_states[hp_idx], data)
                _LOGGER.debug("DEBUG-008: Completed tracking energy consumption for HP%s", hp_idx)

            _LOGGER.debug("DEBUG-009: Completed _track_energy_consumption")

        except Exception as ex:
            _LOGGER.error("DEBUG-ERROR: Error tracking energy consumption: %s", ex)
            import traceback
            _LOGGER.error("DEBUG-ERROR: Traceback in _track_energy_consumption: %s", traceback.format_exc())

    async def _track_hp_energy_consumption(self, hp_idx, current_state, data):
        """Track energy consumption for a specific heat pump (both electrical and thermal)."""
        _LOGGER.debug("DEBUG-010: Entering _track_hp_energy_consumption for HP%s", hp_idx)
        try:
            # --- ELECTRICAL ENERGY (existing logic) ---
            await self._track_hp_energy_type_consumption(
                hp_idx, current_state, data,
                sensor_type="electrical",
                default_sensor_id_template="sensor.{name_prefix}_hp{hp_idx}_compressor_power_consumption_accumulated",
                unit_check_fn=self._is_energy_unit,
                convert_to_kwh_fn=self._convert_energy_to_kwh_cached,
                last_reading_dict=self._last_energy_reading,
                first_value_seen_dict=self._energy_first_value_seen,
                increment_fn=self._increment_energy_consumption
            )

            # --- THERMAL ENERGY (new logic) ---
            await self._track_hp_energy_type_consumption(
                hp_idx, current_state, data,
                sensor_type="thermal",
                default_sensor_id_template="sensor.{name_prefix}_hp{hp_idx}_compressor_thermal_energy_output_accumulated",
                unit_check_fn=self._is_energy_unit,  # Assume same unit check for now
                convert_to_kwh_fn=self._convert_energy_to_kwh_cached,  # Assume same conversion for now
                last_reading_dict=getattr(self, '_last_thermal_energy_reading', {}),
                first_value_seen_dict=getattr(self, '_thermal_energy_first_value_seen', {}),
                increment_fn=getattr(self, '_increment_thermal_energy_consumption', None)
            )
        except Exception as ex:
            _LOGGER.error("Error tracking energy consumption for HP%s: %s", hp_idx, ex)

    def _default_internal_energy_entity_id(self, hp_idx, sensor_type):
        """Namensbasierte entity_id unseres eigenen akkumulierten Energie-Sensors.

        Bewusst OHNE Registry-Auflösung: dieser Wert wird mit der persistierten
        sensor_id verglichen (detect_sensor_change) und wieder persistiert. Würde er
        über die Registry aufgelöst, meldeten Bestandsanlagen, deren real registrierte
        entity_id vom Namen abweicht, beim ersten Start nach dem Update einen
        Sensor-Wechsel und setzten damit die Energie-Basislinie neu.

        Für den Lesepfad (dort ist die reale entity_id nötig) gibt es
        _resolve_internal_energy_sensor_entity_id().
        """
        name_prefix = normalize_name_prefix(self.entry.data.get("name", "")) or "eu08l"
        sensor_id = INTERNAL_ENERGY_SENSOR_IDS[sensor_type]
        return f"sensor.{name_prefix}_hp{hp_idx}_{sensor_id}"

    def _resolve_internal_energy_sensor_entity_id(
        self, hp_idx, sensor_type, default_sensor_id_template
    ):
        """Ermittelt die entity_id unseres eigenen akkumulierten Energie-Sensors.

        Die entity_id wird NICHT aus dem Gerätenamen rekonstruiert, sondern über die
        unique_id in der Entity Registry nachgeschlagen. Grund: die unique_id wird seit
        jeher mit normalize_name_prefix() gebildet und ist damit stabil, während die
        tatsächliche entity_id je nach Anlagezeitpunkt (bzw. nach manueller Umbenennung
        durch den Nutzer) abweichen kann. Ein Rekonstruieren aus dem Namen schlägt bei
        Sonderzeichen im Gerätenamen fehl (z.B. "Lambda_EU10L" -> entity_id enthält den
        Unterstrich, die Namens-Slugifizierung aber nicht).

        Fällt auf die bisherige namensbasierte Konstruktion zurück, wenn die Entity
        (noch) nicht in der Registry steht - z.B. im ersten Zyklus nach dem Start.
        """
        name_prefix = slugify_name_prefix_for_lookup(self.entry.data.get("name", "")) or "eu08l"
        fallback_entity_id = default_sensor_id_template.format(
            name_prefix=name_prefix, hp_idx=hp_idx
        )

        sensor_id = INTERNAL_ENERGY_SENSOR_IDS.get(sensor_type)
        if not sensor_id:
            return fallback_entity_id

        try:
            unique_id_prefix = normalize_name_prefix(self.entry.data.get("name", "")) or "eu08l"
            names = generate_sensor_names(
                f"hp{hp_idx}",
                sensor_id,  # display name irrelevant für die unique_id
                sensor_id,
                unique_id_prefix,
                self._use_legacy_names,
            )
            registry = self._entity_registry or async_get_entity_registry(self.hass)
            return resolve_entity_id_by_unique_id(
                self.hass, names["unique_id"], fallback_entity_id, entity_registry=registry
            )
        except Exception as ex:  # pragma: no cover - defensiv, Registry darf nie den Poll killen
            _LOGGER.debug(
                "[Energy] HP%s %s: Registry-Lookup fehlgeschlagen (%s), verwende Fallback",
                hp_idx, sensor_type, ex,
            )
            return fallback_entity_id

    async def _track_hp_energy_type_consumption(
        self, hp_idx, current_state, data, sensor_type, default_sensor_id_template,
        unit_check_fn, convert_to_kwh_fn, last_reading_dict, first_value_seen_dict, increment_fn
    ):
        """Generic tracking for electrical or thermal energy sensors."""
        # Get sensor configuration for this heat pump (optional)
        hp_key = f"hp{hp_idx}"
        sensor_config = self._energy_sensor_configs.get(hp_key, {})
        sensor_entity_id = sensor_config.get(f"{sensor_type}_sensor_entity_id")
        if not sensor_entity_id and sensor_type == "electrical":
            # Fallback: generischer sensor_entity_id aus Config (nur für elektrisch)
            sensor_entity_id = sensor_config.get("sensor_entity_id")
        if not sensor_entity_id:
            sensor_entity_id = self._resolve_internal_energy_sensor_entity_id(
                hp_idx, sensor_type, default_sensor_id_template
            )
            _LOGGER.debug(
                "[Energy] HP%s %s: Verwende Modbus-Sensor %s",
                hp_idx, sensor_type, sensor_entity_id,
            )
        # Get current energy reading from the configured sensor
        current_energy_state = self.hass.states.get(sensor_entity_id)
        if not current_energy_state or current_energy_state.state in ["unknown", "unavailable"]:
            _LOGGER.debug(
                "[Energy] HP%s %s: Sensor %s nicht verfügbar (state=%s)",
                hp_idx, sensor_type, sensor_entity_id,
                current_energy_state.state if current_energy_state else "None",
            )
            return
        try:
            current_energy = float(current_energy_state.state)
        except (ValueError, TypeError):
            return
        unit = current_energy_state.attributes.get("unit_of_measurement", "")
        cache_key = f"{sensor_type}_hp{hp_idx}"
        if not hasattr(self, '_energy_unit_cache_all'):
            self._energy_unit_cache_all = {}
        if cache_key not in self._energy_unit_cache_all:
            if not unit_check_fn(unit):
                self._energy_unit_cache_all[cache_key] = None
                return
            else:
                self._energy_unit_cache_all[cache_key] = unit
        elif self._energy_unit_cache_all[cache_key] != unit:
            if not unit_check_fn(unit):
                self._energy_unit_cache_all[cache_key] = None
                return
            else:
                self._energy_unit_cache_all[cache_key] = unit
        if self._energy_unit_cache_all[cache_key] is None:
            return
        cached_unit = self._energy_unit_cache_all[cache_key]
        original_energy = current_energy
        current_energy_kwh = convert_to_kwh_fn(current_energy, cached_unit)
        # Get last energy reading for this heat pump
        last_energy = last_reading_dict.get(f"hp{hp_idx}", None)
        first_value_seen = first_value_seen_dict.get(f"hp{hp_idx}", False)
        if current_energy_kwh == 0.0:
            first_value_seen_dict[f"hp{hp_idx}"] = False
            return
        if not first_value_seen or last_energy is None:
            last_reading_dict[f"hp{hp_idx}"] = current_energy_kwh
            first_value_seen_dict[f"hp{hp_idx}"] = True
            await self._persist_counters()
            return
        from .utils import calculate_energy_delta
        if current_energy_kwh < last_energy:
            first_value_seen_dict[f"hp{hp_idx}"] = False
            last_reading_dict[f"hp{hp_idx}"] = None
            await self._persist_counters()
            return
        energy_delta = calculate_energy_delta(current_energy_kwh, last_energy)
        if energy_delta is None:
            # Implausibles Delta verworfen - Referenz neu setzen, kein Inkrement buchen
            last_reading_dict[f"hp{hp_idx}"] = current_energy_kwh
            await self._persist_counters()
            return
        if energy_delta < 0:
            return
        last_reading_dict[f"hp{hp_idx}"] = current_energy_kwh
        # Get last operating state for this heat pump
        last_state = self._energy_last_operating_state.get(str(hp_idx), 0)
        mode_mapping = {
            0: "stby", 1: "heating", 2: "hot_water", 3: "cooling", 4: "stby", 5: "defrost",
        }
        if current_state in mode_mapping:
            mode = mode_mapping[current_state]
        else:
            mode = "stby"
        if current_state != last_state:
            if increment_fn:
                _LOGGER.info(
                    "[Energy] HP%s Modus %s: Inkrement %.4f kWh (Modbus: %.2f -> %.2f kWh)",
                    hp_idx, mode, energy_delta, last_energy, current_energy_kwh,
                )
                await increment_fn(hp_idx, mode, energy_delta)
        else:
            if mode == "stby" or energy_delta > 0:
                if increment_fn:
                    _LOGGER.debug(
                        "[Energy] HP%s Modus %s: Inkrement %.4f kWh (Modbus: %.2f -> %.2f kWh)",
                        hp_idx, mode, energy_delta, last_energy, current_energy_kwh,
                    )
                    await increment_fn(hp_idx, mode, energy_delta)
        self._energy_last_operating_state[str(hp_idx)] = current_state

    async def _increment_energy_consumption(self, hp_idx, mode, energy_delta):
        """Increment energy consumption for a specific mode and heat pump."""
        try:
            from .utils import increment_energy_consumption_counter
            
            # Get energy offsets for this heat pump
            hp_key = f"hp{hp_idx}"
            _LOGGER.debug("DEBUG-014: Getting energy offsets for %s", hp_key)
            energy_offsets = self._energy_offsets.get(hp_key, {})
            _LOGGER.debug("DEBUG-015: Energy offsets for %s: %s", hp_key, energy_offsets)
            _LOGGER.debug("DEBUG-016: Type of energy_offsets: %s", type(energy_offsets))
            
            # Get name prefix from entry data
            name_prefix = normalize_name_prefix(self.entry.data.get("name", "")) or "eu08l"

            # Increment both total and daily counters
            await increment_energy_consumption_counter(
                hass=self.hass,
                mode=mode,
                hp_index=hp_idx,
                energy_delta=energy_delta,
                name_prefix=name_prefix,
                use_legacy_modbus_names=self._use_legacy_names,
                energy_offsets=energy_offsets,
            )

        except Exception as ex:
            _LOGGER.error("Error incrementing energy consumption for HP%s %s: %s", hp_idx, mode, ex)

    def _on_ha_started(self, event):
        """Handle Home Assistant started event."""
        self._ha_started = True
        _LOGGER.debug(
            "Home Assistant started - enabling room temperature and PV surplus updates"
        )
        self._start_fast_poll()

    def _start_fast_poll(self) -> None:
        """Register the fast polling timer for edge detection."""
        if self._unsub_fast_poll is not None:
            return
        fast_interval = self.entry.options.get("fast_update_interval", DEFAULT_FAST_UPDATE_INTERVAL)
        _LOGGER.info(
            "Starting fast edge-detection poll at %ds interval", fast_interval
        )
        self._unsub_fast_poll = async_track_time_interval(
            self.hass,
            self._async_fast_update,
            timedelta(seconds=fast_interval),
        )

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator."""
        _LOGGER.debug("Shutting down Lambda coordinator")
        try:
            # Stop periodic updates by unsubscribing from refresh callback
            # This prevents new refresh tasks from being created
            if hasattr(self, "_unsub_refresh") and self._unsub_refresh:
                try:
                    self._unsub_refresh()
                    self._unsub_refresh = None
                    _LOGGER.debug("Stopped periodic refresh updates")
                except Exception as unsub_ex:
                    _LOGGER.debug("Error unsubscribing from refresh: %s", unsub_ex)

            if self._unsub_fast_poll is not None:
                try:
                    self._unsub_fast_poll()
                    self._unsub_fast_poll = None
                    _LOGGER.debug("Stopped fast edge-detection polling")
                except Exception as unsub_ex:
                    _LOGGER.debug("Error unsubscribing fast poll: %s", unsub_ex)
            
            # Close the Modbus link, which cancels any request in flight.
            await self._close_connection()


        except Exception as ex:
            _LOGGER.error("Error during coordinator shutdown: %s", ex)

    async def _load_sensor_overrides(self) -> dict[str, str]:
        """Load sensor name overrides from YAML config file."""
        config_path = os.path.join(self._config_dir, "lambda_wp_config.yaml")
        if not os.path.exists(config_path):
            return {}
        try:

            def _read_config():
                with open(config_path) as f:
                    content = f.read()
                    config = yaml.safe_load(content) or {}
                    overrides = {}
                    for sensor in config.get("sensors_names_override", []):
                        if "id" in sensor and "override_name" in sensor:
                            overrides[sensor["id"]] = sensor["override_name"]
                    return overrides

            return await self.hass.async_add_executor_job(_read_config)
        except Exception as e:
            _LOGGER.error("Fehler beim Laden der Sensor-Namen-Überschreibungen: %s", e)
            return {}



