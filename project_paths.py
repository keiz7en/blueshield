"""Shared paths for the build and test scripts.

The version lives in one place (``EXTENSION_VERSION`` in ``build.py``) so that
bumping it does not require touching the test harnesses.
"""

from __future__ import annotations

import re
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
