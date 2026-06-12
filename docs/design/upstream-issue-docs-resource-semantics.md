# DRAFT — upstream issue for home-assistant/developers.home-assistant

> Repo: home-assistant/developers.home-assistant · Type: docs gap
> Status: draft for review, not filed

---

**Title:** Document frontend module loading-order semantics: Lovelace
resources are awaited, `extra_module_url` / `add_extra_js_url` is not

## Summary

The [registering resources](https://developers.home-assistant.io/docs/frontend/custom-ui/registering-resources/)
page documents the two manual ways to register a card module (the Resources
UI and the `lovelace: resources:` YAML block), but doesn't mention the
loading-order guarantee that makes resources the right mechanism — nor warn
about the alternative that looks equally valid and isn't.

There are effectively two ways JS modules reach the frontend, with very
different semantics:

- **Lovelace resources** are awaited as part of loading a dashboard: by the
  time any card renders, the module has been fetched and evaluated. This is
  what HACS uses for every card it installs, which is why HACS cards load
  deterministically.
- **`frontend.extra_module_url`** (and its Python counterpart
  `homeassistant.components.frontend.add_extra_js_url`, which is the most
  discoverable API surface for an integration bundling its own cards) is
  fire-and-forget: dashboards render without waiting for these modules.

An integration author who ships cards via `add_extra_js_url` gets a
load-order race: warm browser caches win it invisibly, cold caches (first
visit, hard refresh, headless render) lose it. The failure modes are
confusing — "Timeout waiting for strategy element …" for dashboard
strategies, and error cards for custom cards (permanent ones in panel views,
see the companion frontend issue) — and effectively unreproducible in a
developer's warmed-up browser.

## Suggested changes

On the registering-resources page (or a new page under the frontend section
for integration authors):

1. State the guarantee: Lovelace resources are loaded before dashboards
   render; `extra_module_url` modules are not, and must not be relied on for
   anything a dashboard references (cards, strategies).
2. Recommend that integrations bundling dashboard modules register them as
   Lovelace resources (storage mode), the way HACS does, and note that
   YAML-mode resource lists are user-managed so integrations should document
   the manual entry instead.
3. Mention version-busting query parameters as the cache-invalidation
   convention for resource URLs.

I'm happy to turn this into a docs PR if the approach sounds right — I've
just been through exactly this in my own integration (shipped cards via
`add_extra_js_url`, spent a day root-causing the intermittent cold-load
failures, switched to registering resources programmatically) so I have the
worked example to write from. The one part I'd want a maintainer's view on
is whether the storage-mode resource collection is considered a supported
surface for integrations, or whether that pattern should be documented with
caveats.
