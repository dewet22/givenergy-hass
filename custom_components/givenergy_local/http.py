"""HTTP views backing the frame-capture issue-report flow (issue #64).

A capture is written to ``<config>/givenergy_local_captures/`` (out of ``/local/``,
so it's never served unauthenticated). The persistent notification links to
:class:`CaptureLandingView`, a single page that lets the user inspect the capture
inline, switch between past captures, download the file, or open a pre-filled
GitHub issue.

Auth note: HA's HTTP stack authenticates via a bearer header or a signed-request
``authSig`` query param — there is no cookie auth, so a plain ``<a href>``
navigation from a notification would otherwise 401. Every link the user follows
(the notification → landing URL, each dropdown entry, the download link) is
therefore individually signed via :func:`async_sign_path` with a ~1h expiry.
"""

from __future__ import annotations

import html
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web
from homeassistant.components.http import HomeAssistantView  # type: ignore[attr-defined]
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant

from .const import DOMAIN

CAPTURE_DIR_NAME = "givenergy_local_captures"

# Captures are named purely from an epoch — no user/inverter input reaches the
# filename, but the views still re-validate against this allowlist so a crafted
# URL can never escape the capture directory (path-traversal-proof).
CAPTURE_FILENAME_RE = re.compile(r"^capture_givenergy_\d+\.txt$")

_SIGNED_URL_TTL = timedelta(hours=1)

_GITHUB_ISSUE_URL = "https://github.com/dewet22/givenergy-hass/issues/new"
_GITHUB_ISSUE_BODY = """\
**What happened?**


**Steps to reproduce**


**Modbus wire capture**
Please attach the capture file you downloaded from this page — drag-and-drop it
into this issue (GitHub doesn't support attaching files via a link). The capture
includes the environment details needed to triage, so there's no need to fill
those in by hand.
"""


def capture_dir(hass: HomeAssistant) -> Path:
    """Directory holding wire captures for this integration."""
    return Path(hass.config.path(CAPTURE_DIR_NAME))


def landing_path(filename: str) -> str:
    return f"/api/{DOMAIN}/capture/{filename}"


def download_path(filename: str) -> str:
    return f"/api/{DOMAIN}/capture/{filename}/download"


def _epoch_from_filename(filename: str) -> int:
    # capture_givenergy_<epoch>.txt — regex-validated before we get here.
    return int(filename[len("capture_givenergy_") : -len(".txt")])


def _split_header(content: str) -> tuple[str, str]:
    """Split a capture into its leading ``# ``-prefixed env header and the body."""
    lines = content.splitlines()
    cut = 0
    for cut, line in enumerate(lines):  # noqa: B007 - cut is used after the loop
        if line and not line.startswith("#"):
            break
    else:
        cut = len(lines)
    return "\n".join(lines[:cut]), "\n".join(lines[cut:])


async def _list_captures(hass: HomeAssistant) -> list[str]:
    """Capture filenames, newest first."""
    directory = capture_dir(hass)

    def _scan() -> list[str]:
        if not directory.is_dir():
            return []
        names = [p.name for p in directory.iterdir() if CAPTURE_FILENAME_RE.match(p.name)]
        return sorted(names, key=_epoch_from_filename, reverse=True)

    return await hass.async_add_executor_job(_scan)


class CaptureLandingView(HomeAssistantView):
    """Landing page for a single capture: inspect, switch, download, file issue."""

    url = "/api/" + DOMAIN + "/capture/{filename}"
    name = f"api:{DOMAIN}:capture"
    requires_auth = True  # accepts a valid bearer header or a signed authSig query

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        if not CAPTURE_FILENAME_RE.match(filename):
            return web.Response(status=404)
        hass = self.hass
        path = capture_dir(hass) / filename
        content = await hass.async_add_executor_job(_read_if_present, path)
        if content is None:
            return web.Response(status=404)

        captures = await _list_captures(hass)
        header, body = _split_header(content)

        options = []
        for name in captures:
            signed = async_sign_path(hass, landing_path(name), _SIGNED_URL_TTL)
            selected = " selected" if name == filename else ""
            options.append(
                f'<option value="{html.escape(signed, quote=True)}" '
                f'data-epoch="{_epoch_from_filename(name)}"{selected}>'
                f"{html.escape(name)}</option>"
            )

        download_url = async_sign_path(hass, download_path(filename), _SIGNED_URL_TTL)
        github_url = (
            _GITHUB_ISSUE_URL
            + "?"
            + urlencode({"title": "", "body": _GITHUB_ISSUE_BODY, "labels": "bug"})
        )

        page = _LANDING_TEMPLATE.format(
            options="\n".join(options),
            header=html.escape(header),
            body=html.escape(body),
            download_url=html.escape(download_url, quote=True),
            github_url=html.escape(github_url, quote=True),
        )
        return web.Response(text=page, content_type="text/html")


class CaptureDownloadView(HomeAssistantView):
    """Serve a capture as a file download (browser cookie-less click or curl)."""

    url = "/api/" + DOMAIN + "/capture/{filename}/download"
    name = f"api:{DOMAIN}:capture:download"
    requires_auth = True  # signed authSig query works here too, so curl can fetch

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, filename: str) -> web.StreamResponse:
        if not CAPTURE_FILENAME_RE.match(filename):
            return web.Response(status=404)
        hass = self.hass
        path = capture_dir(hass) / filename
        content = await hass.async_add_executor_job(_read_if_present, path)
        if content is None:
            return web.Response(status=404)
        return web.Response(
            text=content,
            content_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


def _read_if_present(path: Path) -> str | None:
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def build_capture_notification_url(hass: HomeAssistant, filename: str) -> str:
    """Signed landing-page URL for the persistent notification link."""
    return async_sign_path(hass, landing_path(filename), _SIGNED_URL_TTL)


_LANDING_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GivEnergy Local — wire capture</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 1.5rem; max-width: 70rem; }}
  h1 {{ font-size: 1.3rem; }}
  pre {{ background: #f5f5f5; padding: 1rem; overflow-x: auto; border-radius: 6px;
         white-space: pre-wrap; word-break: break-all; }}
  .env {{ background: #eef4ff; }}
  .actions a {{ display: inline-block; margin-right: 1rem; padding: 0.5rem 1rem;
                background: #03a9f4; color: #fff; text-decoration: none;
                border-radius: 6px; }}
  .actions a.github {{ background: #24292f; }}
  select {{ padding: 0.4rem; }}
</style>
</head>
<body>
<h1>GivEnergy Local — Modbus wire capture</h1>

<form>
  <label>Capture:
    <select id="capture-select" onchange="window.location = this.value;">
{options}
    </select>
  </label>
</form>

<pre class="env">{header}</pre>

<p class="actions">
  <a href="{download_url}">Download capture</a>
  <a class="github" href="{github_url}" target="_blank" rel="noopener">Open a GitHub issue</a>
</p>

<h2 style="font-size:1.1rem">Captured frames</h2>
<pre>{body}</pre>

<script>
  // Render each capture's epoch in the viewer's locale; the value stays a signed URL.
  for (const opt of document.querySelectorAll('#capture-select option')) {{
    const epoch = Number(opt.dataset.epoch);
    if (epoch) opt.textContent = new Date(epoch * 1000).toLocaleString();
  }}
</script>
</body>
</html>
"""
