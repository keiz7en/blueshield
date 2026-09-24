# BlueShield

`BlueShield` is a single-package Chromium Manifest V3 extension that combines a
content blocker with automatic tracker protection, ships one light-blue theme,
and exposes **one** settings page for every option.

Internally it bundles two open-source engines, unmodified in the areas that
matter for their licenses:

- **Blocking engine** — the Chromium MV3 build of the project in `../uBlock/`
  (uBlock Origin Lite `1.75.1.0`, commit `d34727edf`)
- **Tracker-protection engine** — the upstream `mv3-chrome` branch checked out at
  `../privacybadger-mv3/` (Privacy Badger `2026.9.15`, commit `84c42d58b`)

The legacy Manifest V2 targets in those repositories are deliberately not used:
modern Chromium requires MV3, and the supplied uBlock repository's supported
Chromium implementation is its MV3 Lite build.

## What the user sees

- Name, icons, popup, dashboard, tracker pages and settings are all **BlueShield**.
- Every page uses one **light-blue** theme; the bundled dark themes are disabled.
- No upstream project name appears anywhere in the interface. The build fails if
  one is found in a page or a localized string.
- One **unified settings page** (`blueshield-settings.html`, also the browser's
  Options page) with seven sections: Overview, Protection, Filter lists, Custom
  rules, Trackers, Data & storage, About. It drives both engines directly.
- Upstream licence texts stay inside the package under `licenses/`, as the GPL
  requires, but they are never displayed in the interface.

## Build

```bash
cd ../uBlock
git submodule update --init --recursive
make mv3-chromium

cd ../blueshield
./build.py
./smoke_test.py
```

`build.py` leaves both upstream repositories unchanged. It:

1. copies the MV3 blocking build to the extension root;
2. copies the tracker engine under `vendor/tracker-protection/`;
3. imports both MV3 background modules into one service worker;
4. namespaces storage, runtime messages, paths, locales, content-script IDs and
   DNR rule IDs so the two engines cannot collide;
5. moves the blocking engine's rebuildable regex rules into a private session
   range, keeping the shared 5,000-rule dynamic quota free for the learned
   tracker rules;
6. retries tracker rules that lose a startup race for the shared quota, and
   reports permanently invalid ones instead of stalling;
7. rebrands every page and localized string, and applies the light-blue theme;
8. merges managed-policy schemas and licences, then builds ZIP, CRX3 and reports.

## Release artifacts

- `release/BlueShield-1.0.0.0/` — tested unpacked extension
- `release/BlueShield-1.0.0.0.zip` — upload/install ZIP, `manifest.json` at root
- `release/BlueShield-1.0.0.0.crx` — CRX3 signed with the local development key
- `release/blueshield-metadata.json` — versions, commits, ID, sizes, hashes
- `release/SHA256SUMS` — ZIP and CRX checksums
- `release/smoke-test.json` — Chromium runtime, blocking, storage and memory report

Extension ID: `jbdmpgnpkidpmibioddiapfgklfncgcf`.
The private key lives at `keys/blueshield-signing.pem`, outside the package.
Back it up if this development identity must receive updates; for public
distribution upload the ZIP through the Chrome Web Store instead.

## Memory use

The extension runs a **single** service worker — there is no background page,
no offscreen document kept alive, and no polling loop. The settings page is only
loaded when opened and is discarded when closed. Measured in Chromium 153 by
`smoke_test.py`:

| Context | Used JS heap |
| --- | --- |
| Service worker | ~31 MB |
| Settings page | ~19 MB |
| Blocking dashboard | ~10 MB |
| Toolbar popup | ~7 MB |

The test fails the build if the worker exceeds 96 MB, the settings or dashboard
pages exceed 64 MB, the popup exceeds 32 MB, or a second background context
appears.

## Validation

`build.py` checks JavaScript syntax, manifest references, absence of private key
material, the CRX3 structure and signature, ZIP/CRX payload parity, the derived
extension ID, and that no upstream brand name survives in any page or locale.

`smoke_test.py` loads the build in Chromium 153 and verifies:

- both engines initialize with no critical errors and no duplicate rule IDs;
- all 4,942 learned tracker rules install (nothing skipped);
- the blocking engine's regex rules sit in their reserved session range;
- both static ruleset sets are enabled;
- a real page load has its advertising **and** analytics requests blocked at the
  network layer while a control request still succeeds;
- settings written from the settings page are stored, read back and restored,
  and "save all settings" flushes every tracker store;
- every UI page opens with the BlueShield name, the light-blue theme, no
  upstream wording and no console errors;
- the unified settings page opens all seven sections and loads live data.

## Installing on Windows

See `install/INSTALL-WINDOWS.md`. In short: Chrome on Windows never installs a
dropped `.crx`, managed machines block anything outside their allow-list, and
from Chrome 137 the `--load-extension` switch needs
`--disable-features=DisableLoadExtensionCommandLineSwitch`. The `install/`
folder contains a launcher, an allow policy and a force-install policy that
cover all three cases.

## Development helpers

- `screenshots.py` captures the popup, settings, dashboard, tracker pages and
  welcome page into `release/screenshots/` for visual review.
- `debug_page.py <path-inside-extension>` prints the live layout state of one
  page (visibility, body display, text length, images, init flags).
