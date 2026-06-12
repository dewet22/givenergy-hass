# DRAFT — upstream issue for home-assistant/frontend

> Repo: home-assistant/frontend · Type: bug report
> Status: FILED as https://github.com/home-assistant/frontend/issues/52570 (2026-06-12)

---

**Title:** Panel views error permanently when a custom card's module is still
loading, while masonry/sections views recover

## Checklist

- [x] I have updated to the latest available Home Assistant version.
- [x] I have cleared the cache of my browser.
- [x] I have tried a different browser to see if it is related to my browser.

## Describe the issue you are experiencing

When a dashboard view references a custom card whose JS module hasn't finished
loading yet, the behaviour depends on the view type:

- In **masonry/sections views**, the card is wrapped in `hui-card`, which
  tolerates the element being defined late — the card appears as soon as the
  module evaluates.
- In **panel views** (`panel: true`), the card element is built directly. If
  the custom element isn't defined at that instant, the view renders an error
  card ("Configuration error" / custom element doesn't exist) and **never
  recovers**, even though the module finishes loading moments later.

The asymmetry makes panel views a load-order lottery for any custom card
that isn't already in the browser cache. A warm reload works; a hard refresh
or first visit can permanently error, which makes the problem look
intermittent and very hard for card/integration authors to reproduce —
attempting to reproduce it in a warmed-up development browser destroys the
evidence.

I found this while debugging my integration's bundled cards (loaded via
`add_extra_js_url`, which the frontend doesn't await): identical card config
rendered fine in a masonry view and permanently errored in a panel view on
cold loads. Registering the modules as Lovelace resources (which *are*
awaited) shrinks the window considerably, but the underlying asymmetry
remains: anything that delays module evaluation past view build leaves a
panel view broken where a masonry view would have healed.

## Describe the behavior you expected

Panel views to tolerate late-defined custom elements the same way `hui-card`
does — render a placeholder and upgrade when the element is defined, rather
than committing permanently to an error card.

## Steps to reproduce the issue

1. Serve a custom card module so that it loads slowly or late — easiest
   deterministic repro: register it via
   `homeassistant.components.frontend.add_extra_js_url` from an integration
   (not as a Lovelace resource), and test against a cold browser profile, or
   add an artificial delay to the module response.
2. Create a dashboard with two views referencing the same card:

   ```yaml
   views:
     - title: Masonry
       cards:
         - type: custom:my-card
     - title: Panel
       panel: true
       cards:
         - type: custom:my-card
   ```

3. Open each view in a fresh browser profile (or Ctrl/Cmd+Shift+R).
4. The masonry view shows the card once the module arrives; the panel view
   shows a permanent error card until the page is reloaded.

A headless browser (fresh profile every load, no cache) reproduces this on
every single load.

## Environment

- Home Assistant 2026.6.2, HA OS
- Reproduced in Chrome (headless and desktop); not browser-specific
- Custom cards involved: bundled with the `givenergy_local` custom
  integration, but the mechanism is generic

## Additional information

Related observation while debugging, possibly worth its own docs issue: the
loading-order semantics of the two module-delivery mechanisms
(`add_extra_js_url` / `extra_module_url` = fire-and-forget vs Lovelace
resources = awaited before dashboards render) don't appear to be documented
anywhere; integration authors who pick the former get this failure mode with
no warning. I've worked around it in my integration by registering the
modules as storage-mode Lovelace resources and additionally awaiting
`customElements.whenDefined` in my dashboard strategy before emitting panel
views.
