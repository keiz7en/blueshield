# BlueShield 1.0.3.0

Closes all eight [AdBlockBench](https://adblockbench.com) leaks — the suite now
scores **100/100** on every signal (was 73) — and makes the build and test
cycle dramatically faster. The extension itself stays error-free across the
full test suite.

## AdBlockBench: 73 → 100

A new harness (`adblockbench_test.py`) runs the full public suite against the
built extension. The eight leaks and their fixes:

**Network (filter lists alone could not close these):**
- DoubleClick ad endpoints reached through `fetch()`: `gampad/ads`,
  `pagead/ads?ad_type=video`, `pagead/viewthroughconversion/`, `mads/gma`;
- `analytics.js` fetched from a blob Worker, where `$script`-scoped rules do
  not apply.

Both are covered by the supplemental ruleset (renamed from
`blueshield-telemetry` to `blueshield-supplemental`), whose rules declare no
`resourceTypes` so a single rule matches pages, Workers and scripts alike.

**API (page-world, `src/blueshield-privacy.js`):**
- Canvas fingerprinting: `getImageData` now flips one color channel of the
  last pixel by ±1 on the returned copy — invisible, but it breaks the
  exact-match comparisons fingerprinters rely on. The canvas itself is never
  touched, so repeated reads stay consistent.
- Audio fingerprinting: the first `getChannelData` read of each `AudioBuffer`
  nudges sample 0 by `1e-7` — far below audibility, exact-match readback is
  broken.
- Privacy Sandbox: the Topics, Protected Audience, Attribution Reporting and
  Private State Tokens entry points are removed so `typeof` is no longer
  `"function"` (and `in` checks stop seeing them). On browsers that already
  dropped these APIs the script is a silent no-op.

This script is registered as a **MAIN-world** content script at
`document_start`, because isolated-world patches are invisible to ordinary page
scripts. `navigator.globalPrivacyControl` is deliberately left untouched: it is
a privacy opt-out signal that must keep reporting true.

The two beacon checks AdBlockBench reports as *inconclusive* are excluded from
its score: server-verified delivery requires both a tracking beacon and the
site's own decoy beacon to arrive, and the enabled lists correctly block that
measurement domain too.

## Faster

| What | Before | After |
| --- | --- | --- |
| Full build | minutes (serial `node --check` per file) | **44.5 s** (checks run in parallel) |
| ZIP compression | level 9 | level 6 — +1.4 KB, ~1/3 the CPU |
| DNR rule install | fixed 200-rule slices | adaptive slices (target ≤ 100 ms, bounded 25–500), so the browser never stalls more than ~100 ms per call and fast machines use fewer, larger round trips |

## Changed

- **New development signing key → new extension ID**
  (`iknhlankjpcfkhdgnopkhbpdpillgokc`). The development key used by releases
  through 1.0.2.0 was never committed and could not be recovered when this
  release was built; every ID reference (README, install policies, smoke test)
  now points at the new ID. Installing 1.0.3.0 over an older build adds a
  *second* extension — remove the old one first, then load the new folder;
  settings made under the old ID are not carried over.
- The test harnesses run on Windows: browser discovery prefers `chromium`,
  then auto-downloads Chrome for Testing (Google Chrome branding removed
  `--load-extension` in Chrome 137 and the kill-switch in 142, so branded
  Chrome cannot load unpacked extensions at all anymore), with temporary
  profiles under the system temp directory.
- Fixed a service-worker bootstrap race in the test harnesses (the worker's
  `chrome` binding appears ~2 s after CDP attach), OpenSSL temp-file locking
  in the build on Windows, and CRX payload validation there too.

## Measured, on this build

| Check | Result |
| --- | --- |
| AdBlockBench (`adblockbench_test.py`) | **100 / 100** — network 100, cosmetic 100, API 100; 0 leaked, 0 page errors |
| TurtleCute AdBlockTest (`adblocktest.py`) | pass |
| Dataset coverage (`coverage_test.py`) | **128 / 128 hosts (100%)**, cosmetic hiding pass |
| Runtime smoke (`smoke_test.py`) | pass — blocking, storage, settings UI, no console errors |
| Per-tab memory (`tab_memory_test.py`) | pass — no per-tab rule or storage growth |
| Sandbox frames (`sandbox_frames_test.py`) | pass — no script refusals, no exceptions |
| Long tasks (`perf_test.py`) | pass — zero stalls ≥ 500 ms |

## Assets

| Asset | Purpose |
| --- | --- |
| `BlueShield-1.0.3.0.zip` | Store upload / normal install |
| `BlueShield-1.0.3.0.crx` | CRX3 signed with the project development key, for managed/force-install deployment |
| `SHA256SUMS` | Checksums for both archives |
| `blueshield-metadata.json` | Versions, source commits, extension ID, sizes, hashes |
| `smoke-test.json` | Chromium runtime verification report |
| `coverage.json` | Blocking coverage report (128 hosts + cosmetic) |
| `adblockbench.json` | AdBlockBench run captured by the new harness |
| `adblocktest.json` | TurtleCute AdBlockTest report |
| `perf.json` | Long-task and request timing report |
| `tab-memory.json` | Per-tab memory and cleanup report |

Extension ID: `iknhlankjpcfkhdgnopkhbpdpillgokc` (new since 1.0.3.0; releases
through 1.0.2.0 used `jbdmpgnpkidpmibioddiapfgklfncgcf`).

## Upgrading

1. Remove the previous BlueShield from `chrome://extensions` (different ID).
2. Load the new unpacked folder (`release\BlueShield-1.0.3.0`) or install the
   new ZIP, then reload the extension. Nothing else is required.
