#!/usr/bin/env python3
"""Reproduce the sandboxed-frame message locally and measure its effect.

Chrome refuses to run content scripts inside a frame whose `sandbox` attribute
lacks `allow-scripts`. The extension registers scriptlets with
`matchOriginAsFallback`, so they are offered to about:blank frames and Chrome
logs the refusal. This test builds that exact situation, then checks whether the
page, the blocking and the cosmetic filtering are affected.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import websocket

from project_paths import ROOT, STAGE

REPORT = ROOT / "release" / "sandbox-frames.json"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>sandbox frames</title></head>
<body>
<!-- A frame the page itself cannot script: no allow-scripts. -->
<iframe sandbox="allow-same-origin" src="/inner.html" width="300" height="80"></iframe>
<!-- The exact shape from the report: a sandboxed frame whose document is
     about:blank, which is what matchOriginAsFallback scripts are offered. -->
<iframe sandbox="allow-same-origin" src="about:blank" width="300" height="80"></iframe>
<iframe sandbox src="about:blank" width="300" height="80"></iframe>
<!-- A frame with scripting allowed, for comparison. -->
<iframe sandbox="allow-scripts allow-same-origin" src="/inner.html" width="300" height="80"></iframe>
<!-- An ordinary frame. -->
<iframe src="/inner.html" width="300" height="80"></iframe>
<div class="inplayer-ad" style="height:40px">ad placeholder</div>
<div id="keep-me" style="height:40px">content</div>
</body></html>
"""

INNER = """<!doctype html>
<html><head><meta charset="utf-8"><title>inner</title></head>
<body>
<div class="advboxemb" style="height:30px">inner ad</div>
<div id="inner-keep" style="height:30px">inner content</div>
</body></html>
"""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class CDP:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(url, timeout=30, origin="http://127.0.0.1")
        self.next_id = 1
        self.events = []

    def send(self, method, params=None, timeout=90):
        request_id = self.next_id
        self.next_id += 1
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.ws.settimeout(max(0.1, deadline - time.monotonic()))
            message = json.loads(self.ws.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
            if "method" in message:
                self.events.append(message)
        raise TimeoutError(method)

    def evaluate(self, expression, await_promise=True):
        result = self.send("Runtime.evaluate", {
            "expression": expression, "awaitPromise": await_promise, "returnByValue": True,
        })
        if result.get("exceptionDetails"):
            raise RuntimeError(json.dumps(result["exceptionDetails"])[:300])
        return result.get("result", {}).get("value")

    def close(self):
        self.ws.close()


def http_json(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def targets(port):
    return http_json(f"http://127.0.0.1:{port}/json/list")


def attach(port, target_id):
    target = next(item for item in targets(port) if item.get("id") == target_id)
    client = CDP(target["webSocketDebuggerUrl"])
    client.send("Runtime.enable")
    client.send("Log.enable")
    return client


def main() -> int:
    report = {}
    with tempfile.TemporaryDirectory(prefix="bs-sandbox-", dir="/tmp/opencode",
                                     ignore_cleanup_errors=True) as temp_name:
        temp = Path(temp_name)
        site = temp / "site"
        site.mkdir()
        (site / "index.html").write_text(PAGE, encoding="utf-8")
        (site / "inner.html").write_text(INNER, encoding="utf-8")
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(site)))
        site_port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        profile = temp / "profile"
        for name in ("home", "config", "cache"):
            (temp / name).mkdir()
        env = dict(os.environ, HOME=str(temp / "home"),
                   XDG_CONFIG_HOME=str(temp / "config"),
                   XDG_CACHE_HOME=str(temp / "cache"))
        command = [
            "chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
            "--no-first-run", f"--user-data-dir={profile}",
            "--remote-debugging-port=0", "--remote-allow-origins=*",
            f"--disable-extensions-except={STAGE}", f"--load-extension={STAGE}",
            "--enable-logging=stderr", "--v=0", "about:blank",
        ]
        with (ROOT / "release" / "chromium-sandbox.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        browser = None
        try:
            port_file = profile / "DevToolsActivePort"
            deadline = time.monotonic() + 30
            port = None
            while time.monotonic() < deadline and port is None:
                if port_file.exists() and port_file.read_text().splitlines():
                    port = int(port_file.read_text().splitlines()[0])
                time.sleep(0.1)
            if port is None:
                raise TimeoutError("devtools endpoint")

            browser = CDP(http_json(f"http://127.0.0.1:{port}/json/version")["webSocketDebuggerUrl"])
            browser.send("Target.setDiscoverTargets", {"discover": True})

            worker_target = None
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and worker_target is None:
                for target in targets(port):
                    if target.get("type") == "service_worker" and \
                            "blueshield-service-worker.js" in target.get("url", ""):
                        worker_target = target
                        break
                time.sleep(0.25)
            if worker_target is None:
                raise RuntimeError("service worker target not found")
            worker = attach(port, worker_target["id"])
            worker.send("Runtime.runIfWaitingForDebugger")
            previous = -1
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                added = worker.evaluate(
                    "(globalThis.__blueshieldTrackerDnrStats || {}).added || 0")
                scripts = worker.evaluate("""(async () =>
                    (await chrome.scripting.getRegisteredContentScripts())
                        .map(s => s.id))()""") or []
                if added >= 4900 and added == previous and "css-generic-all" in scripts:
                    break
                previous = added
                time.sleep(1.5)
            report["registered"] = scripts
            worker.close()

            target_id = browser.send("Target.createTarget", {"url": "about:blank"})["targetId"]
            page = attach(port, target_id)
            page.send("Page.enable")
            page.send("Network.enable")
            page.send("Page.navigate", {"url": f"http://127.0.0.1:{site_port}/index.html"})
            time.sleep(12)

            console = []
            for event in page.events:
                if event.get("method") == "Runtime.consoleAPICalled":
                    params = event["params"]
                    console.append({
                        "type": params.get("type"),
                        "text": " ".join(
                            str(arg.get("value") or arg.get("description", ""))
                            for arg in params.get("args", []))[:300],
                    })
                elif event.get("method") == "Log.entryAdded":
                    entry = event["params"]["entry"]
                    console.append({
                        "type": entry.get("level"),
                        "text": str(entry.get("text", ""))[:300],
                        "source": entry.get("source"),
                    })
            exceptions = [
                str(event["params"].get("exceptionDetails", {}).get("text"))[:200]
                for event in page.events
                if event.get("method") == "Runtime.exceptionThrown"
            ]

            sandbox = [item for item in console
                       if "sandboxed" in item["text"] and "allow-scripts" in item["text"]]
            report["sandboxMessages"] = len(sandbox)
            report["sandboxSample"] = sandbox[0] if sandbox else None
            report["otherConsole"] = [item for item in console if item not in sandbox][:10]
            report["exceptions"] = exceptions

            # Did the refusal cost us anything? Cosmetic filtering must still work
            # in the top document and in the scriptable frames.
            report["effect"] = page.evaluate("""(() => {
                const hidden = el => {
                    if (!el) { return 'missing'; }
                    const s = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return (s.display === 'none' || s.visibility === 'hidden'
                        || r.height === 0) ? 'hidden' : 'visible';
                };
                const frames = Array.from(document.querySelectorAll('iframe'));
                return {
                    topAd: hidden(document.querySelector('.inplayer-ad')),
                    topContent: hidden(document.querySelector('#keep-me')),
                    frames: frames.map(f => {
                        let inner = null;
                        try {
                            const doc = f.contentDocument;
                            inner = doc && hidden(doc.querySelector('.advboxemb'));
                        } catch (e) { inner = 'inaccessible'; }
                        return { sandbox: f.getAttribute('sandbox'), innerAd: inner };
                    }),
                };
            })()""", False)
            report["ok"] = True
            page.close()
        finally:
            server.shutdown()
            if browser is not None:
                browser.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print("sandbox refusal messages:", report.get("sandboxMessages"))
    if report.get("sandboxSample"):
        print("  ", report["sandboxSample"]["text"][:220])
    print("exceptions:", report.get("exceptions"))
    print("effect:", json.dumps(report.get("effect")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
