# BlueShield 1.0.0.0

Single-package Chromium Manifest V3 extension: content blocking plus automatic
tracker protection, one name, one light-blue theme, one settings page.

## What is in this release

| Asset | Purpose |
| --- | --- |
| `BlueShield-1.0.0.0.zip` | Store upload / normal install. `manifest.json` is at the archive root. |
| `BlueShield-1.0.0.0.crx` | CRX3 signed with the project development key, for managed/force-install deployment. |
| `SHA256SUMS` | Checksums for both archives. |
| `smoke-test.json` | Chromium 153 verification report for this exact build. |
| `blueshield-metadata.json` | Component versions, source commits, extension ID, sizes and hashes. |

Extension ID: `jbdmpgnpkidpmibioddiapfgklfncgcf` (stable across reloads and
releases, so stored settings survive updates).

## Install

**Chrome / Edge, personal machine** — unzip and load the folder:

1. `chrome://extensions` → enable **Developer mode**
2. **Load unpacked** → pick the unzipped folder (the one containing `manifest.json`)
3. Open **Settings** from the toolbar popup

**Chrome 137+** ignores `--load-extension` unless the feature is re-enabled;
`install/launch-dev-windows.bat` in the repository handles that, and also uses a
separate profile so your main profile is untouched.

**Managed machines** — the extension can be force-installed with the policies in
`install/`. If Chrome is managed by your organisation, the extension must be
removed from `ExtensionInstallBlocklist` (or added to
`ExtensionInstallAllowlist`) by IT; a local change cannot override that.

**Anyone, including non-technical users** — upload `BlueShield-1.0.0.0.zip` to
the Chrome Web Store (or Edge Add-ons). A self-signed CRX is a development
artifact; the Web Store is the only "store authorised" channel.

## Verification for this build

Run in Chromium 153 against the packaged extension:

- both engines initialise; 4,942 learned tracker rules install with **0 skipped
  and 0 duplicate IDs**; 949 session rules; 10 static rulesets enabled
- a real page load has its advertising **and** analytics requests blocked at the
  network layer, while a control request still succeeds
- settings changed from the settings page are written to `chrome.storage.local`,
  read back, restored, and "Save all settings" flushes every tracker store
  (2.79 MB, 23 entries)
- every UI page renders with the BlueShield name, the light-blue theme, no
  upstream wording and no console errors
- heap use: service worker 31 MB, settings page 20 MB, dashboard 10 MB,
  popup 7 MB (one shared service worker, no background page)

## Notes

- All settings and learned tracker data stay in the local browser profile.
  Nothing is sent to the developers of this build.
- The bundled components remain under their original licences; the full texts
  ship inside the extension under `licenses/` and are not shown in the UI.
- This is an independent build and is not endorsed by the upstream projects.
