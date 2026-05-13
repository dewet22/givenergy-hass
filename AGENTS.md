# givenergy-hass

Home Assistant custom integration (not an addon) for GivEnergy inverters. Depends on
givenergy-modbus as its core library.

## Purpose
- Exposes inverter state and controls as Home Assistant entities
- Polls the inverter directly over local Modbus TCP (no cloud required)
- Integration domain: `givenergy_local` — governs all entity/config IDs

## Structure
- `custom_components/givenergy_local/` — all integration code
- `coordinator.py` — DataUpdateCoordinator; centralises all Modbus polling
- `sensor.py`, `number.py`, `select.py`, `switch.py`, `time.py` — entity platforms
- `config_flow.py` — UI setup (IP, port, scan interval, battery count, passive mode)

## Key Notes
- Depends on givenergy-modbus — mind breaking changes in that library
- Home Assistant custom integration conventions apply (config flow, entity registry, etc.)
- Requires HA 2025.4+, Python 3.13+
- Changes to entity names/IDs are breaking for existing HA installations
- Be conservative with polling frequency — querying too often disrupts cloud metrics
  (slave address 0x11 interaction with GivEnergy cloud is a known sensitivity)
- Passive mode exists: listen-only when another Modbus client is already polling

## Testing
- `uv run pytest` — uses pytest-homeassistant-custom-component
- `uv run mypy custom_components` — strict mode
- Run `uv run ruff check --fix && uv run ruff format` before committing
