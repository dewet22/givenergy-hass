# Security Review — givenergy-hass

- **Date:** 2026-06-10
- **Scope:** Full codebase at commit `488b26c` (`feat(sensor): expose per-module AIO battery devices (#147)`)
- **Method:** Manual review of all security-relevant surfaces, plus pattern scans for dangerous constructs (code-injection sinks, unsafe deserialisation, shell execution, committed secrets)
- **Result:** **No exploitable vulnerabilities found.** Two low-severity, defense-in-depth observations noted below.

## Summary

This is a small (~7,400 line) Home Assistant custom integration that communicates with GivEnergy inverters over local Modbus TCP. The review found consistently strong security hygiene across every attack surface examined: strict input allowlisting on the HTTP views, systematic output escaping in both the server-rendered landing page and the frontend card, schema-validated service inputs, and exemplary GitHub Actions hardening.

## Attack surfaces reviewed

### HTTP file-serving views — `custom_components/givenergy_local/http.py`

The highest-risk surface: serves Modbus wire-capture files over HTTP via a landing page (`CaptureLandingView`) and a download endpoint (`CaptureDownloadView`). Hardened correctly on every axis:

- **Path traversal:** filenames are validated against a strict allowlist regex (`^capture_givenergy_\d+\.txt$`) *before* any filesystem access, so traversal is structurally impossible — even though no user input reaches capture filenames in the first place (they are epoch-generated server-side).
- **Authentication:** both views set `requires_auth = True`; every link the user follows (notification → landing page, dropdown entries, download link) is individually signed via `async_sign_path` with a 1-hour TTL.
- **XSS:** all dynamic content in the landing-page HTML is passed through `html.escape()` — the env header, the capture body, and the signed URLs (with `quote=True` for attribute contexts).
- **Unauthenticated static serving:** captures live in `<config>/givenergy_local_captures/`, outside `/local/`, so they are never served by HA's unauthenticated static path.
- **Header injection:** the `Content-Disposition` filename is regex-constrained.

### Frontend module — `custom_components/givenergy_local/www/ge-strategy.js`

2,090 lines with four `innerHTML` sinks (heatmap card, flow card, glance card). All inverter-derived strings (battery serials, pack names) are run through an `esc()` helper encoding `& < > " '` before interpolation. Numeric values pass through `fmtKw`/`fmtKwh`/`toFixed` formatters. SVG attributes are built only from numbers and hardcoded colour constants. No `eval`, `new Function`, or `document.write`.

### Service handlers — `custom_components/givenergy_local/__init__.py`

All services (`reboot_inverter`, `calibrate_battery_soc`, `set_system_datetime`, `capture_frames`, `redetect_plant`, `expose_recommended_entities`) use `voluptuous` schemas with bounded inputs — e.g. capture `duration` is coerced to int and clamped to 10–300 s. The persistent-notification HTML interpolates only a server-generated signed URL, never user input. The capture header (`_build_capture_header`) deliberately includes no inverter serial/model/firmware.

### Config flow — `custom_components/givenergy_local/config_flow.py`

Schema-validated host/port/interval inputs; connection-test failures are logged without leaking anything sensitive; reconfigure refuses to silently re-point an entry at a different inverter. No issues.

### Migration script — `scripts/migrate_from_givtcp.py`

Standalone admin CLI using a user-supplied long-lived token against a user-supplied HA URL — a trusted-operator model by design. Default TLS verification is intact (no `verify=False` / unverified SSL contexts). No `yaml.load`, `pickle`, `subprocess`, `eval`, or `shell=True` anywhere in the repository's Python.

### GitHub Actions — `.github/workflows/`

Genuinely exemplary:

- All actions SHA-pinned to full commit hashes.
- Default token scoped to `contents: read`; `persist-credentials: false` on all checkouts in `validate.yml`.
- `bump-givenergy-modbus.yml` accepts an untrusted `repository_dispatch` payload, but passes it through an environment variable (never inline `${{ }}` interpolation into `run:`) and validates it against a strict PEP 440-ish regex before it touches any git ref, commit message, or `GITHUB_OUTPUT` — closing both shell-injection and output-injection vectors. The validation comment documents the trust boundary explicitly.
- No `pull_request_target` usage.

### Secrets

Pattern scan for committed credentials (API keys, tokens, passwords) found nothing.

## Low-severity observations (not vulnerabilities)

1. **Capture files are readable by any authenticated HA user.** `requires_auth = True` admits any valid bearer token, not just admins, and capture filenames are epoch-based (guessable within a narrow window). A non-admin HA user could enumerate and download wire captures. This matches Home Assistant's standard "authenticated = trusted" model, and captures are already redacted (serial numbers zeroed by the library; no serial/model/firmware in the header), so impact is minimal. Gating on admin would be a tightening option, but the current posture is reasonable.

2. **Capture files are written with default permissions (0644).** Other local OS users on the HA host could read them — the same trust boundary as the rest of the HA config directory, so no worse than the surrounding system.

## Conclusion

A clean, security-conscious codebase. Recent commit history (SHA-pinning actions, `persist-credentials: false`, safe notification-link handling) shows active attention to security, and it is reflected consistently throughout the code.
