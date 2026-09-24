# BlueShield on Windows — why it "turns itself off", and how to fix it

The BlueShield build itself is fine: it is a valid Manifest V3 package and the
same files load on Linux. What differs on Windows is **which Chrome you are
running and which policies it obeys**. There are four common causes; work
through them in order.

## 0. Diagnose in 60 seconds

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Open `chrome://policy` and press **Reload policies**.
3. Read the two things that matter:
   - `chrome://version` → *Command line* and *Profile Path*. If a policy
     argument or `--disable-extensions` is present, something manages Chrome.
   - `chrome://policy` → look for **`ExtensionInstallBlocklist`**, **`ExtensionInstallAllowlist`**, **`ExtensionSettings`**, and any policy **marked with a source other than "policy"** (those come from your organisation and you cannot override them yourself).
4. Click the extension's **Errors** button on `chrome://extensions`; a
   "This extension is not from the Web Store" or "blocked by your organisation"
   message is the answer to your question.

---

## 1. You are running branded Chrome and dragged the `.crx` in

Chrome removed CRX sideloading years ago: **dropping a `.crx` on the window or
double-clicking it never works on Windows.** The CRX we build is for store
upload and for managed deployment, not for double-click install.

**Fix — load the unpacked folder instead:**

* `chrome://extensions` → enable **Developer mode** → **Load unpacked** →
  select `release\BlueShield-<version>` (the folder that contains
  `manifest.json`, *not* the `.zip`).

The extension ID stays `jbdmpgnpkidpmibioddiapfgklfncgcf` in every case, so
settings and stored data are preserved across reloads.

## 2. "Load unpacked" is missing, greyed out, or Chrome starts with no toolbar

From **Chrome 137 onward** the `--load-extension` switch is disabled by
default, and managed profiles can hide the Developer mode toggle.

**Fix — use the bundled launcher, which handles both:**

```bat
install\launch-dev-windows.bat
install\launch-dev-windows.bat C:\path\to\BlueShield-<version>
```

It launches Chrome for Testing (preferred) or Chrome with:

* a **separate** profile directory, so your normal profile is untouched;
* `--load-extension="<unpacked folder>"`;
* `--disable-features=DisableLoadExtensionCommandLineSwitch` — **required from
  Chrome 137**, otherwise the switch is silently ignored and no extension loads;
* `chrome://extensions/?errors=1` so any error is shown immediately.

If Chrome for Testing is not installed, get it from
<https://googlechromelabs.github.io/chrome-for-testing/> — it has no Web Store
gating and is the most reliable way to load an unpacked extension on Windows.

## 3. Chrome is managed by your school/company (most common "auto off")

A machine-wide policy blocks anything that is not allow-listed, and
Chrome disables it again on every start. This is why it looks like the extension
"turns itself off".

**Check:** `chrome://policy`. If you see `ExtensionInstallBlocklist` with `*`,
or `ExtensionSettings` for your ID with `"blocked"`, that is the cause.

**Fix — run as administrator:** `install\blueshield-allow-policy.reg`

It removes BlueShield from the blocklist, marks it `allowed`, and stops the
blanket ban on non-Web-Store downloads. Then restart Chrome and reload policies.

If the policy is coming from your organisation (it shows up in the "Source"
column as something other than *policy*), you cannot fix it locally — send IT
this exact request:

> Please remove extension `jbdmpgnpkidpmibioddiapfgklfncgcf` from
> `ExtensionInstallBlocklist`, or add it to `ExtensionInstallAllowlist`, and
> make sure `ExtensionSettings` does not block it.

## 4. You want it on machines you manage, without touching each one

**Fix — force-install from a URL:** `install\blueshield-force-install-policy.reg`

1. Serve the CRX over http/https (Chrome refuses `file://`):

   ```bat
   cd C:\inetpub\wwwroot
   py -m http.server 8000
   ```

2. Replace `http://YOUR-SERVER-HERE:8000/...` in the `.reg` with the real URL.
3. Run it as administrator and restart Chrome.

Chrome then installs and updates BlueShield on every profile. Our CRX is already
CRX3-signed with the key that produces the ID in the list, which is why this
works.

For Chromium-based builds (including Chrome for Testing) you can instead drop
`install\external-extensions-windows.json` plus the CRX into
`…\Application\<version>\External Extensions\`. Branded Chrome on Windows
ignores that folder — use the policy route there.

---

## 5. If you want it for normal users (the "authorized" route)

A self-signed CRX is a *development* artifact: Chrome will never treat it as
Web-Store authorised, so any Chrome installation that enforces store
signatures will refuse it. The only routes that are genuinely "authorized":

* upload `BlueShield-<version>.zip` to the **Chrome Web Store** (it can stay
  "unlisted" — still signed by Google, still installs without warnings);
* **Edge Add-ons** for Microsoft Edge;
* or managed deployment through the policies above, inside an organisation you
  control.

## 6. Nothing above helped

* Confirm the folder really contains `manifest.json` at its top level.
* Unzip to a path **without spaces or non-ASCII characters** (`C:\blueshield\`),
  e.g. a network drive or a path with permissions problems can stop the
  extension from loading.
* Antivirus/EDR can block `chrome.exe` flags or quarantine files in the profile
  directory; check the Windows Security quarantine and your AV event log.
* Run the launcher with a fresh profile: if it works there, the problem is in
  your normal profile's policies or state, not in the build.
