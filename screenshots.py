#!/usr/bin/env python3
"""Capture screenshots of the BlueShield UI for visual review."""
import json, os, subprocess, tempfile, time, urllib.request
from pathlib import Path
import websocket

from project_paths import ROOT, STAGE

OUT = ROOT / "release" / "screenshots"
OUT.mkdir(exist_ok=True)


class CDP:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=10, origin="http://127.0.0.1")
        self.i = 1

    def send(self, method, params=None, timeout=30):
        i = self.i; self.i += 1
        self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.ws.settimeout(max(0.1, end - time.monotonic()))
            msg = json.loads(self.ws.recv())
            if msg.get("id") == i:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})
        raise TimeoutError(method)


def targets(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
        return json.load(r)


def main():
    with tempfile.TemporaryDirectory(prefix="bs-shot-", dir="/tmp/opencode") as tmp:
        tmp = Path(tmp)
        prof = tmp / "p"
        env = dict(os.environ, HOME=str(tmp))
        proc = subprocess.Popen([
            "chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
            f"--user-data-dir={prof}", "--remote-debugging-port=0", "--remote-allow-origins=*",
            f"--disable-extensions-except={STAGE}", f"--load-extension={STAGE}",
            "--window-size=1280,900", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        port_file = prof / "DevToolsActivePort"
        for _ in range(200):
            if port_file.exists() and port_file.read_text().splitlines():
                port = int(port_file.read_text().splitlines()[0]); break
            time.sleep(0.1)
        else:
            raise TimeoutError("devtools")

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version") as r:
            browser = CDP(json.load(r)["webSocketDebuggerUrl"])
        browser.send("Target.setDiscoverTargets", {"discover": True})

        eid = None
        for _ in range(200):
            for t in targets(port):
                if t.get("type") == "service_worker" and "blueshield-service-worker.js" in t.get("url", ""):
                    eid = t["url"].split("/")[2]
            if eid:
                break
            time.sleep(0.2)
        if not eid:
            raise TimeoutError("extension")

        pages = {
            "popup": (f"chrome-extension://{eid}/blueshield-popup.html", 380, 520, False),
            "settings": (f"chrome-extension://{eid}/blueshield-settings.html", 1280, 900, False),
            "settings-lists": (f"chrome-extension://{eid}/blueshield-settings.html#lists", 1280, 900, False),
            "dashboard": (f"chrome-extension://{eid}/dashboard.html", 1280, 900, True),
            "tracker-options": (f"chrome-extension://{eid}/vendor/tracker-protection/skin/options.html", 1280, 900, True),
            "tracker-report": (f"chrome-extension://{eid}/vendor/tracker-protection/skin/popup.html", 520, 640, True),
            "welcome": (f"chrome-extension://{eid}/vendor/tracker-protection/skin/firstRun.html", 1000, 900, True),
        }
        for name, (url, w, h, settle) in pages.items():
            tid = browser.send("Target.createTarget", {"url": "about:blank"})["targetId"]
            t = next(x for x in targets(port) if x["id"] == tid)
            c = CDP(t["webSocketDebuggerUrl"])
            c.send("Page.enable")
            c.send("Emulation.setDeviceMetricsOverride",
                   {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
            c.send("Page.navigate", {"url": url})
            deadline = time.time() + (25 if settle else 6)
            while time.time() < deadline:
                time.sleep(1)
                try:
                    vis = c.send("Runtime.evaluate", {"expression": "getComputedStyle(document.documentElement).visibility", "returnByValue": True})["result"]["value"]
                    length = c.send("Runtime.evaluate", {"expression": "document.body.innerText.length", "returnByValue": True})["result"]["value"]
                    if vis == "visible" and length > 40:
                        break
                except Exception:
                    pass
            shot = c.send("Page.captureScreenshot", {"format": "png"})["data"]
            import base64
            (OUT / f"{name}.png").write_bytes(base64.b64decode(shot))
            print("captured", name)
            c.ws.close()
        browser.ws.close()
        proc.terminate(); proc.wait(timeout=10)


if __name__ == "__main__":
    main()
