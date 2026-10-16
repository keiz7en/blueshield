"""Shared paths and browser discovery for the build and test scripts.

The version lives in one place (``EXTENSION_VERSION`` in ``build.py``) so that
bumping it does not require touching the test harnesses.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _build_constant(name: str, fallback: str) -> str:
    text = (ROOT / "build.py").read_text(encoding="utf-8")
    match = re.search(rf'^{name}\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        return fallback
    return match.group(1)


VERSION = _build_constant("EXTENSION_VERSION", "0.0.0.0")
EXTENSION_NAME = _build_constant("EXTENSION_NAME", "BlueShield")
STAGE_NAME = f"{EXTENSION_NAME}-{VERSION}"
RELEASE = ROOT / "release"
STAGE = RELEASE / STAGE_NAME


def scratch_root() -> Path:
    """Directory for harness temporary profiles and datasets.

    ``%TEMP%`` on Windows, ``/tmp`` elsewhere (previously a hardcoded
    ``/tmp/opencode`` that only existed on the development machine).
    """
    root = Path(tempfile.gettempdir()) / "blueshield-tests"
    root.mkdir(parents=True, exist_ok=True)
    return root


_CHROME_FOR_TESTING_MANIFEST = (
    "https://googlechromelabs.github.io/chrome-for-testing/"
    "last-known-good-versions-with-downloads.json"
)
_chrome_for_testing_path: str | None = None


def _chrome_for_testing() -> str | None:
    """Download (once per machine) and return Chrome for Testing's binary.

    Google Chrome branded builds removed ``--load-extension`` (Chrome 137,
    kill-switch dropped again in Chrome 142) and ``--disable-extensions-except``
    (Chrome 139), so the harnesses run against Chrome for Testing, which keeps
    both switches and the websocket DevTools endpoint. The browser is cached
    under the scratch root and reused across runs.
    """
    global _chrome_for_testing_path
    if _chrome_for_testing_path:
        return _chrome_for_testing_path
    cache = scratch_root() / "chrome-for-testing"
    if sys.platform == "win32":
        platform_key, exe_parts = "win64", ("chrome-win64", "chrome.exe")
    elif sys.platform.startswith("linux"):
        platform_key, exe_parts = "linux64", ("chrome-linux64", "chrome")
    elif sys.platform == "darwin":
        platform_key, exe_parts = "mac-x64", (
            "chrome-mac-x64", "Google Chrome for Testing.app", "Contents", "MacOS",
            "Google Chrome for Testing",
        )
    else:
        return None
    exe = cache.joinpath(*exe_parts)
    marker = cache / ".version"
    if exe.is_file() and marker.is_file():
        _chrome_for_testing_path = str(exe)
        return _chrome_for_testing_path
    import json
    import urllib.request
    with urllib.request.urlopen(_CHROME_FOR_TESTING_MANIFEST, timeout=60) as response:
        manifest = json.load(response)
    channel = manifest["channels"]["Stable"]
    version = channel["version"]
    downloads = channel.get("downloads", {}).get("chrome", [])
    url = next((item["url"] for item in downloads if item["platform"] == platform_key), None)
    if not url:
        return None
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / "download.zip"
    urllib.request.urlretrieve(url, archive)  # noqa: S310 - fixed official host
    shutil.unpack_archive(str(archive), str(cache))
    archive.unlink()
    marker.write_text(version, encoding="utf-8")
    if not exe.is_file():
        raise RuntimeError(f"Chrome for Testing unpack failed: missing {exe}")
    _chrome_for_testing_path = str(exe)
    return _chrome_for_testing_path


def blueshield_browser() -> list[str]:
    """Command prefix that launches a Chromium-based browser for the harnesses.

    Resolution order: ``BLUESHIELD_BROWSER`` env var, ``chromium`` on PATH
    (the Linux development setup), Chrome for Testing (auto-downloaded; the
    only supported option on Windows, since Google Chrome branding blocks
    ``--load-extension``), then the standard Google Chrome install locations
    as a last resort.
    """
    override = os.environ.get("BLUESHIELD_BROWSER")
    candidates: list[str | None] = [override] if override else []
    candidates += [shutil.which("chromium")]
    if not any(candidates):
        # Only hit the network when no ready-made non-branded browser exists.
        candidates.append(_chrome_for_testing())
    candidates += [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        command = [str(candidate)]
        name = Path(candidate).name.lower()
        if "chromium" not in name and "chrome" in name:
            command.append("--disable-features=DisableLoadExtensionCommandLineSwitch")
        return command
    raise RuntimeError(
        "No Chromium-based browser found; set BLUESHIELD_BROWSER to its path."
    )
