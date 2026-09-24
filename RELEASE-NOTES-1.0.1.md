# BlueShield 1.0.1.0

Patch release focused on **per-tab memory and background-tab behaviour**, plus
version-robust tooling.

## Fixed

**Per-tab state is now released 2 seconds after a tab closes** (was 20 seconds).
The tracker engine kept each closed tab's learned trackers, temporary allow
lists and per-tab session rules for 20 seconds, so rapid tab churn made the
shared service worker hold data for every recently closed tab. The shorter delay
is still long enough for the close event's queue to drain.

## Measured, on this build

`tab_memory_test.py` opens 13 tabs, lets the tracker engine learn on each, then
closes them all and checks the extension returns to its baseline:

| Metric | Result |
| --- | --- |
| Worker heap per open tab | **~23 KB** (29.6 MB → 29.9 MB) |
| Tracker data per open tab | **~185 bytes** (2.4 KB for 12 tabs) |
| Per-tab DNR rules | **0** — blocking is global, not per tab |
| Left behind after closing every tab | **none** (tab entries 12 → 0) |
| Re-activating a background tab | **~1 ms** script round-trip |

The test fails the build if rules, tab entries or storage grow after tabs close,
or if a single tab costs more than 1 MB of worker heap.

**If a background tab still feels frozen, that is Chrome, not this extension.**
Chrome's Memory Saver unloads background tabs and they must reload when you
return. See *Installing on Windows* / the README for the settings that keep them
warm. When such a tab does reload it is protected from the first request, because
blocking is declarative.

## Changed

- The build, smoke test, screenshot and debug tools read the version from
  `build.py` through the new `project_paths.py`, so bumping the version no longer
  requires editing four scripts.
- `install/launch-dev-windows.bat` now auto-detects the newest
  `release\BlueShield-*` folder, so it never points at a stale version.

## Assets

| Asset | Purpose |
| --- | --- |
| `BlueShield-1.0.1.0.zip` | Store upload / normal install |
| `BlueShield-1.0.1.0.crx` | CRX3 signed with the project development key, for managed/force-install deployment |
| `SHA256SUMS` | Checksums for both archives |
| `smoke-test.json` | Chromium 153 verification report for this build |
| `tab-memory.json` | Per-tab memory and cleanup report for this build |
| `blueshield-metadata.json` | Versions, source commits, extension ID, sizes, hashes |

Extension ID is unchanged (`jbdmpgnpkidpmibioddiapfgklfncgcf`), so installing
this release over 1.0.0.0 keeps all settings and learned tracker data.

## Upgrading

Load the new unpacked folder over the old one (or install the new ZIP), then
reload the extension in `chrome://extensions`. Nothing else is required.
