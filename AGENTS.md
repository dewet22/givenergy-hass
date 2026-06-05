# givenergy-hass

Home Assistant custom integration (not an addon) for GivEnergy inverters. Depends on
givenergy-modbus as its core library.

## Purpose
- Exposes inverter state and controls as Home Assistant entities
- Polls the inverter directly over local Modbus TCP (no cloud required)
- Integration domain: `givenergy_local` — governs all entity/config IDs

## Structure
- `custom_components/givenergy_local/` — all integration code
- `coordinator.py` — DataUpdateCoordinator; centralises all Modbus polling via
  `client.refresh()` + `client.load_config()` (full refresh every N ticks)
- `sensor.py`, `number.py`, `select.py`, `switch.py`, `time.py` — entity platforms
- `config_flow.py` — UI setup (IP, port, scan interval, battery count, passive mode)
- `dashboard.py` — `generate_dashboard` service; produces a Lovelace YAML file
- `www/` — bundled frontend assets (e.g. `ge-cell-heatmap.js`) served at `/local/`

## Key Notes
- Depends on givenergy-modbus — mind breaking changes in that library
- Home Assistant custom integration conventions apply (config flow, entity registry, etc.)
- Requires HA 2025.4+, Python 3.14.2+
- Changes to entity names/IDs are breaking for existing HA installations
- Be conservative with polling frequency — querying too often disrupts cloud metrics
  (device address 0x11 is the EMS; its interaction with the GivEnergy cloud is a known
  sensitivity)
- Passive mode exists: listen-only when another Modbus client is already polling

## Entity gating patterns
Two conditional-creation patterns exist in `async_setup_entry`; follow them when adding
entities that only apply to certain plant topologies:

```python
# EMS-only entities (time slots, SoC targets, Flexi EMS switch)
if coordinator.data.ems is not None:
    ...

# AC-coupled-only entities (AC charge/discharge limits)
caps = coordinator.data.capabilities
if caps is not None and caps.is_ac_coupled and not caps.is_three_phase:
    ...
```

## Working conventions

### Imports
- `EntityCategory` must be imported from `homeassistant.const`, and `DeviceInfo` from
  `homeassistant.helpers.device_registry` — **not** `homeassistant.helpers.entity`,
  whose re-exports trigger a mypy `attr-defined` error.

### Dependency pin
The givenergy-modbus version constraint lives in **two files that must stay in sync**:
- `custom_components/givenergy_local/manifest.json` (`requirements`)
- `pyproject.toml` (`dependencies`)

After editing either, run `uv sync --refresh-package givenergy-modbus` to regenerate
`uv.lock`. The `--refresh` flag avoids stale-cache "only <=X available" errors.

### mypy baseline
The integration is **mypy-clean (0 errors)** under strict mode, enforced by the `mypy`
job in `validate.yml` and a prek `uv run mypy` local hook. Keep it clean — fix type
errors at the source rather than adding `# type: ignore`. The one justified ignore is
`http.py`'s `HomeAssistantView` import, which HA doesn't re-export from any public path.

### Repo boundaries
This repo has its own dedicated agent. The sister repos **givenergy-modbus** and
**givenergy-cli** each have their own agents too. If a task requires a change in a sister
repo, do not reach into that repo directly. Instead, write a **handoff markdown file**
that specifies the API boundary the integration will depend on — what symbols/behaviours
are needed, what must not regress, and what is deferred. Be outcome-focused, not
implementation-prescriptive. Park the hass-side change and wait for the sister repo's
pre-release before wiring it up here.

## Release tracks
- **`main`** — 1.1.x line, pinned to givenergy-modbus 2.1.x. Pre-releases use `rcN`/`aN`/`bN` suffixes.
- **`v1.0`** branch — 1.0.x stable/maintenance line, pinned to givenergy-modbus 2.0.x.

Releases are **tag-driven**: push an annotated `v*` tag and `release.yml` builds and
publishes the HACS zip. The tag message becomes the release blurb. A lightweight
(non-annotated) tag will fail the workflow. Pushing tags and merging PRs requires
explicit user confirmation — do not do either without it.

## PR workflow
- PRs are automatically reviewed by **Codex** (`dewet22-codex`). Do not merge before
  its review lands — absence of feedback is not approval.
- When reviewers leave inline comments, use the `/address-review` skill to work through
  them (reply to each thread and resolve) rather than ad-hoc `gh api` calls.
- For any text posted in public under the user's name (issue comments, tag messages, PR
  bodies), show a draft and wait for explicit sign-off before posting or creating the
  artefact.
- Documentation-only changes (README, comments, no code/test impact) can be
  straight-merged without a PR.

## Testing
- `uv run pytest` — uses pytest-homeassistant-custom-component
- `uv run mypy custom_components` — strict mode; must stay clean (0 errors), gated in CI and prek
- Run `uv run ruff check --fix && uv run ruff format` before committing
