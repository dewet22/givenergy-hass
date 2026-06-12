"""Tests for the frame-capture issue-report flow (issue #64).

The capture service writes a header-prefixed file to
``<config>/givenergy_local_captures/`` and posts a persistent notification
linking to a signed landing page. Two authenticated views serve the page and a
download; both reject anything outside the strict capture-filename allowlist.
"""

from __future__ import annotations

import re

import pytest
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.givenergy_local import _build_capture_header
from custom_components.givenergy_local.const import DOMAIN, SERVICE_CAPTURE_FRAMES
from custom_components.givenergy_local.http import (
    CAPTURE_FILENAME_RE,
    _split_header,
    build_capture_notification_url,
    capture_dir,
)


@pytest.fixture(autouse=True)
def _isolate_capture_dir(hass):
    """The test config dir is shared/persistent, so wipe captures around each test."""
    import shutil

    directory = capture_dir(hass)
    shutil.rmtree(directory, ignore_errors=True)
    yield
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
async def capture_setup(hass, mock_client, mock_config_entry):
    """Set up the integration with the http component available.

    Signing capture URLs and registering the capture views both need
    ``http`` up *before* the integration's ``async_setup`` runs, so unlike the
    shared ``setup_integration`` fixture this sets up http first.
    """
    assert await async_setup_component(hass, "http", {})
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


def _make_capture_sink(frames: list[str]):
    """Return an async stand-in for client.capture_frames that emits `frames`."""

    async def _capture_frames(sink, duration):  # noqa: ARG001
        for line in frames:
            direction, _, data = line.partition(": ")
            sink(direction, bytes.fromhex(data))

    return _capture_frames


# ---------------------------------------------------------------------------
# Header builder + helpers (no web server needed)
# ---------------------------------------------------------------------------


async def test_build_capture_header_contains_env_and_no_serial(hass):
    header = await _build_capture_header(
        hass, generated=dt_util.now(), duration=60.0, frame_count=247
    )
    assert "# GivEnergy Local — Modbus wire capture" in header
    assert "# Duration:       60s" in header
    assert "# Frames:         247" in header
    assert "# Home Assistant:" in header
    assert "# Python:" in header
    assert "# OS:" in header
    assert "# Integration:" in header
    assert "# Library:        givenergy-modbus" in header
    # Redaction principle: never an inverter serial in the shared header.
    assert "SA1234G123" not in header


async def test_build_capture_header_version_lookup_off_loop(hass):
    """The package-metadata lookup does blocking filesystem I/O and must run
    in the executor, not on the event loop — HA's blocking-call detector
    flags it (three warnings per capture) and asks users to file a bug."""
    import threading
    from unittest.mock import patch

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def _fake_version(_name: str) -> str:
        seen["thread"] = threading.get_ident()
        return "9.9.9"

    with patch("importlib.metadata.version", _fake_version):
        header = await _build_capture_header(
            hass, generated=dt_util.now(), duration=1.0, frame_count=0
        )

    assert "givenergy-modbus 9.9.9" in header
    assert seen["thread"] != loop_thread


def test_split_header_separates_env_block_from_body():
    content = "# GivEnergy\n# Generated: x\n#\nTX: 0102\nRX: 0304\n"
    header, body = _split_header(content)
    assert header == "# GivEnergy\n# Generated: x\n#"
    assert body == "TX: 0102\nRX: 0304\n".strip("\n")


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("capture_givenergy_1717500000.txt", True),
        ("capture_givenergy_0.txt", True),
        ("capture_givenergy_.txt", False),
        ("capture_givenergy_abc.txt", False),
        ("../secrets.yaml", False),
        ("capture_givenergy_1.txt.bak", False),
        ("dashboard_givenergy_x.yaml", False),
    ],
)
def test_capture_filename_allowlist(name, ok):
    assert bool(CAPTURE_FILENAME_RE.match(name)) is ok


# ---------------------------------------------------------------------------
# Service: writes a capture file + posts a landing-page notification
# ---------------------------------------------------------------------------


async def test_capture_dir_created_at_setup(hass, capture_setup):
    assert capture_dir(hass).is_dir()


async def test_capture_frames_writes_file_with_header(hass, mock_client, capture_setup):
    mock_client.capture_frames.side_effect = _make_capture_sink(["TX: 0102", "RX: 0304"])
    await hass.services.async_call(DOMAIN, SERVICE_CAPTURE_FRAMES, {"duration": 10}, blocking=True)
    files = list(capture_dir(hass).glob("capture_givenergy_*.txt"))
    assert len(files) == 1
    content = files[0].read_text()
    assert content.startswith("# GivEnergy Local")
    assert "# Frames:         2" in content
    assert "TX: 0102" in content and "RX: 0304" in content


async def test_capture_frames_posts_landing_notification(hass, mock_client, capture_setup):
    mock_client.capture_frames.side_effect = _make_capture_sink([])
    await hass.services.async_call(DOMAIN, SERVICE_CAPTURE_FRAMES, {"duration": 10}, blocking=True)
    notifications = hass.data.get("persistent_notification", {})
    note = next((n for nid, n in notifications.items() if "givenergy_capture_" in nid), None)
    assert note is not None, "expected a givenergy_capture_* notification"
    message = note["message"]
    assert f"/api/{DOMAIN}/capture/" in message
    assert "authSig=" in message  # signed landing link
    # Raw <a target="_blank">, not a markdown link: a markdown link is hijacked
    # by the HA frontend SPA router (lands on the dashboard); a truthy target
    # makes the router skip the click so the browser hits the backend view.
    assert 'target="_blank"' in message
    assert "](/api/" not in message  # not a markdown link


async def test_notification_url_is_origin_relative(hass, capture_setup):
    """The notification link must be path-only — no scheme/host/origin.

    Embedding HA's internal URL (e.g. via get_url) breaks reverse-proxied
    installs: the browser sits on the proxy origin while the link points at the
    internal http:// address, tripping an insecure-connection warning. A
    path-only signed URL resolves against whatever origin the user is on.
    """
    url = build_capture_notification_url(hass, "capture_givenergy_1700000000.txt")
    assert url.startswith(f"/api/{DOMAIN}/capture/")
    assert "://" not in url  # origin-relative: no scheme or host


# ---------------------------------------------------------------------------
# Views (served over the authenticated test client)
# ---------------------------------------------------------------------------


@pytest.fixture
async def captured(hass, mock_client, capture_setup):
    """Run a capture and return its filename."""
    mock_client.capture_frames.side_effect = _make_capture_sink(["TX: dead", "RX: beef"])
    await hass.services.async_call(DOMAIN, SERVICE_CAPTURE_FRAMES, {"duration": 10}, blocking=True)
    (path,) = list(capture_dir(hass).glob("capture_givenergy_*.txt"))
    return path.name


async def test_landing_view_renders_page(hass, hass_client, captured):
    client = await hass_client()
    resp = await client.get(f"/api/{DOMAIN}/capture/{captured}")
    assert resp.status == 200
    text = await resp.text()
    assert "GivEnergy Local — Modbus wire capture" in text
    assert captured in text  # capture caption names this file
    assert "dead" in text and "beef" in text  # frames embedded inline
    assert "/issues/new" in text  # GitHub link


async def test_landing_view_rejects_bad_filename(hass, hass_client, capture_setup):
    client = await hass_client()
    resp = await client.get(f"/api/{DOMAIN}/capture/evil.txt")
    assert resp.status == 404


async def test_download_view_serves_attachment(hass, hass_client, captured):
    client = await hass_client()
    resp = await client.get(f"/api/{DOMAIN}/capture/{captured}/download")
    assert resp.status == 200
    assert resp.headers["Content-Disposition"] == f'attachment; filename="{captured}"'
    assert "dead" in await resp.text()


async def test_download_view_rejects_bad_filename(hass, hass_client, capture_setup):
    client = await hass_client()
    resp = await client.get(f"/api/{DOMAIN}/capture/evil.txt/download")
    assert resp.status == 404


async def test_download_via_signed_url_without_auth(hass, hass_client_no_auth, captured):
    """A signed download URL works without an auth header (curl from a terminal)."""
    from datetime import timedelta

    from homeassistant.components.http.auth import async_sign_path

    signed = async_sign_path(hass, f"/api/{DOMAIN}/capture/{captured}/download", timedelta(hours=1))
    client = await hass_client_no_auth()
    resp = await client.get(signed)
    assert resp.status == 200
    assert "dead" in await resp.text()


async def test_unsigned_download_without_auth_is_rejected(hass, hass_client_no_auth, captured):
    client = await hass_client_no_auth()
    resp = await client.get(f"/api/{DOMAIN}/capture/{captured}/download")
    assert resp.status == 401


# ---------------------------------------------------------------------------
# Access hardening (security review 2026-06-10, issue #149)
# ---------------------------------------------------------------------------


async def test_non_admin_bearer_is_rejected(
    hass, hass_client, hass_read_only_access_token, captured
):
    """A non-admin bearer token must not be able to read captures directly —
    epoch-based filenames are guessable, so plain `requires_auth` would let any
    authenticated user enumerate them."""
    client = await hass_client(hass_read_only_access_token)
    resp = await client.get(f"/api/{DOMAIN}/capture/{captured}")
    assert resp.status == 403
    resp = await client.get(f"/api/{DOMAIN}/capture/{captured}/download")
    assert resp.status == 403


async def test_non_admin_with_junk_authsig_is_rejected(
    hass, hass_client, hass_read_only_access_token, captured
):
    """A junk authSig appended to a non-admin bearer request must not pass the
    gate — the middleware authenticates via the bearer header, so the request
    resolves to the bearer's own token, not the content user's."""
    client = await hass_client(hass_read_only_access_token)
    resp = await client.get(f"/api/{DOMAIN}/capture/{captured}?authSig=junk")
    assert resp.status == 403


async def test_notification_link_works_without_auth(
    hass, hass_client_no_auth, mock_client, capture_setup
):
    """End-to-end: the URL in the persistent notification opens the landing page
    with no bearer token at all (browser navigation) — the signed link is the
    capability."""
    mock_client.capture_frames.side_effect = _make_capture_sink(["TX: dead"])
    await hass.services.async_call(DOMAIN, SERVICE_CAPTURE_FRAMES, {"duration": 10}, blocking=True)
    notifications = hass.data.get("persistent_notification", {})
    note = next(n for nid, n in notifications.items() if "givenergy_capture_" in nid)
    href = re.search(r'href="([^"]+)"', note["message"]).group(1)
    client = await hass_client_no_auth()
    resp = await client.get(href)
    assert resp.status == 200
    assert "GivEnergy Local — Modbus wire capture" in await resp.text()


async def test_capture_written_private(hass, mock_client, capture_setup):
    """Capture files land 0600 and the capture dir 0700 — no reason to be
    readable beyond the HA user on a multi-user host."""
    mock_client.capture_frames.side_effect = _make_capture_sink(["TX: 0102"])
    await hass.services.async_call(DOMAIN, SERVICE_CAPTURE_FRAMES, {"duration": 10}, blocking=True)
    directory = capture_dir(hass)
    (path,) = list(directory.glob("capture_givenergy_*.txt"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700


async def test_authorized_fails_closed_when_token_ids_absent(hass):
    """None must never satisfy the signer comparison: a request with no resolved
    token id — and/or no content-user token in hass.data — fails closed rather
    than matching None == None (review feedback on #150)."""
    from homeassistant.components.http.const import KEY_HASS_REFRESH_TOKEN_ID

    from custom_components.givenergy_local.http import _authorized

    # Bare hass: http not set up, so no content-user token exists either side.
    assert _authorized(hass, {}) is False
    # Token id present but no signer token registered: still rejected.
    assert _authorized(hass, {KEY_HASS_REFRESH_TOKEN_ID: "some-token"}) is False


async def test_landing_page_shows_only_the_requested_capture(
    hass, hass_client_no_auth, hass_client, captured
):
    """The landing page renders exactly the one capture it was asked for and
    never names the rest of the history — there's no switcher to enumerate
    from, whether reached via a signed link or an admin bearer (review on
    #150). Each capture is reached via its own notification link."""
    other = "capture_givenergy_1700000000.txt"
    (capture_dir(hass) / other).write_text("# GivEnergy\nTX: 99\n")

    signed = build_capture_notification_url(hass, captured)
    client = await hass_client_no_auth()
    resp = await client.get(signed)
    assert resp.status == 200
    text = await resp.text()
    assert captured in text  # the requested capture
    assert other not in text  # no enumeration of history

    admin = await hass_client()
    resp = await admin.get(f"/api/{DOMAIN}/capture/{captured}")
    text = await resp.text()
    assert captured in text
    assert other not in text  # admins don't get a switcher either


def test_write_capture_tightens_existing_file_mode(tmp_path):
    """O_CREAT's mode applies only on creation: overwriting a pre-existing
    broader-mode file (same-second epoch collision) must still end up 0600
    (review on #150)."""
    from custom_components.givenergy_local.http import write_capture

    path = tmp_path / "capture_givenergy_1700000000.txt"
    path.write_text("old")
    path.chmod(0o644)
    write_capture(path, "new")
    assert path.read_text() == "new"
    assert path.stat().st_mode & 0o777 == 0o600
