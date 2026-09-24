#!/usr/bin/env python3
"""Debug helper: dump the live state of a BlueShield page."""
import json, os, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
import websocket

ROOT = Path(__file__).resolve().parent
STAGE = ROOT / "release" / "BlueShield-1.0.0.0"
page_path = sys.argv[1]


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
            m = json.loads(self.ws.recv())
            if m.get("id") == i:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})
        raise TimeoutError(method)

    def ev(self, expr, await_promise=False):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                            "awaitPromise": await_promise})
        return r.get("result", {}).get("value")


def targets(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
        return json.load(r)


with tempfile.TemporaryDirectory(prefix="bs-dbg-", dir="/tmp/opencode") as tmp:
    tmp = Path(tmp); prof = tmp / "p"
    proc = subprocess.Popen([
        "chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
        f"--user-data-dir={prof}", "--remote-debugging-port=0", "--remote-allow-origins=*",
        f"--disable-extensions-except={STAGE}", f"--load-extension={STAGE}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ, HOME=str(tmp)))
    pf = prof / "DevToolsActivePort"
    for _ in range(200):
        if pf.exists() and pf.read_text().splitlines():
            port = int(pf.read_text().splitlines()[0]); break
        time.sleep(0.1)
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
    tid = browser.send("Target.createTarget", {"url": f"chrome-extension://{eid}/{page_path}"})["targetId"]
    t = next(x for x in targets(port) if x["id"] == tid)
    c = CDP(t["webSocketDebuggerUrl"])
    c.send("Runtime.enable"); c.send("Log.enable")
    for _ in range(20):
        time.sleep(1)
        vis = c.ev("getComputedStyle(document.documentElement).visibility")
        if vis == "visible":
            break
    time.sleep(2)
    print(json.dumps({
        "url": page_path,
        "htmlVisibility": c.ev("getComputedStyle(document.documentElement).visibility"),
        "htmlInlineStyle": c.ev("document.documentElement.getAttribute('style')"),
        "bodyDisplay": c.ev("getComputedStyle(document.body).display"),
        "bodyVisibility": c.ev("getComputedStyle(document.body).visibility"),
        "innerTextLength": c.ev("document.body.innerText.length"),
        "textSample": c.ev("document.body.innerText.slice(0,200)"),
        "optionsInitialized": c.ev("window.OPTIONS_INITIALIZED"),
        "popupInitialized": c.ev("window.POPUP_INITIALIZED"),
        "tabsRect": c.ev("(() => { const e=document.querySelector('#tabs'); if(!e) return null; const r=e.getBoundingClientRect(); return {w:r.width,h:r.height,top:r.top,display:getComputedStyle(e).display,visibility:getComputedStyle(e).visibility}; })()"),
        "errors": [e for e in c.ws_events] if hasattr(c, "ws_events") else None,
    }, indent=1))
    browser.ws.close(); proc.terminate(); proc.wait(timeout=10)
