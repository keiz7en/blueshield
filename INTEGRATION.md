# BlueShield integration design

## Component baselines

- The extension root is the generated Chromium MV3 build of the project in
  `../uBlock/` (uBlock Origin Lite).
- The tracker-protection component comes from Privacy Badger's real
  `origin/mv3-chrome` implementation, not the checked-out MV2 `master` branch.
- The final package is a modified distribution and retains both projects' GPL
  notices under `licenses/`.

## One MV3 service worker

Manifest V3 permits one background service worker. `blueshield-service-worker.js`
imports:

1. the Privacy Badger MV3 background module graph; then
2. the uBlock Origin Lite MV3 background module graph.

Each Privacy Badger ES module imports a component-scoped API facade. The facade
gives Privacy Badger its own view of:

- extension paths (`vendor/tracker-protection/...`);
- runtime messages tagged with `privacybadger`;
- locale keys prefixed with `pb_`;
- local/sync/session storage keys prefixed with `privacybadger.`;
- dynamic content-script IDs prefixed with `privacybadger.`;
- dynamic/session DNR rule views restricted to Privacy Badger's ID ranges.

The facade does not replace the global Chromium APIs used by uBlock. It also
retries tracker rules that transiently lose the shared dynamic-rule quota during
startup, and fails fast on permanently invalid rules instead of retrying them.

## DNR ownership

Chromium exposes a shared 5,000-rule dynamic quota (for host-access rules) to the
extension. Privacy Badger's seed data needs 4,942 dynamic rules, so the blocking
engine's rebuildable regex rules are moved out of the dynamic range.

Explicit ranges:

- Privacy Badger dynamic: `100000000`–`119999999`
- Privacy Badger session: `120000000`–`139999999`
- Privacy Badger static IDs: offset by `200000000`
- uBlock rebuilt regex session: `2000000`–`2999999`
- uBlock strict-block session: `1000000`–`1999999`

Large Privacy Badger updates are sent to Chromium in chunks of 500; a rejected
chunk is bisected down to single rules, which are then retried with a short
backoff when the failure is a quota race. Surrogate redirect rules keep the
leading `/` in `action.redirect.extensionPath`, which Chromium's schema
requires (verified empirically).

## Content scripts and messages

uBlock's original content-script refresh unregistered every dynamic script.
BlueShield preserves IDs beginning with `privacybadger.` and removes only
uBlock-owned IDs. uBlock also refreshes its owned scripts on every startup
instead of assuming an empty global registry.

Privacy Badger's DNT/GPC script is registered as `privacybadger.dnt_signal`.
Runtime messages are tagged and filtered so the two message protocols cannot
answer each other's requests.

## UI and action ownership

A toolbar action can have only one popup. BlueShield therefore ships:

- `blueshield-popup.html` — toolbar popup: per-site status, pause/resume, links;
- `blueshield-settings.html` — the unified settings page (also `options_ui`);
- `popup.html`, `dashboard.html` — the blocking engine's own pages, rebranded and
  themed;
- `vendor/tracker-protection/skin/popup.html` and `.../options.html` — the
  tracker's own pages, rebranded and themed.

The unified settings page talks to both engines through the same message
interfaces their own pages use, so every option is reachable from one place.

Privacy Badger action-mutating methods are scoped as no-ops so it cannot
overwrite uBlock's icon or badge.

## Rebranding

`rebrand_ui()` runs after the merge and before packaging:

- rewrites every localized string (and drops the non-English tracker strings,
  which fall back to the scrubbed English ones, since a translated product name
  cannot be detected reliably);
- replaces the upstream about pane, logos, donate/share/report-to-upstream
  blocks and window titles;
- renames the DOM ids and bundled library file that carry upstream names, in the
  markup, stylesheets and scripts together;
- disables the bundled dark themes and injects `blueshield-theme.css`, the
  light-blue skin linked last in every page;
- `assert_no_upstream_names_in_ui()` then fails the build if any upstream name
  survives in a page or locale.

## Policy and licenses

The two managed-storage schemas are merged into `blueshield-managed-schema.json`.
Full licence and attribution files are included under `licenses/` and are not
rendered in the UI.
