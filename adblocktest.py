#!/usr/bin/env python3
"""Run the TurtleCute AdBlockTest against BlueShield and report what fails.

The site checks three things: whether known ad/tracker hosts are blocked,
whether cosmetic ad elements are hidden, and whether ad scripts fail to load.
This harness loads it in Chromium with the extension enabled, reads the
verdict, and lists every failing check so the coverage gaps can be fixed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websocket

from project_paths import ROOT, STAGE

REPORT = ROOT / "release" / "adblocktest.json"
SITE = "https://adblock.turtlecute.org/"


class CDP:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(url, timeout=30, origin="http://127.0.0.1")
        self.next_id = 1
        self.events = []

    def send(self, method, params=None, timeout=60):
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
            raise RuntimeError(json.dumps(result["exceptionDetails"])[:400])
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


COLLECT = r"""(() => {
    const text = document.body.innerText || '';
    const rows = Array.from(document.querySelectorAll('li, tr, .item, .row, [class*=test], [class*=result]'))
        .map(node => (node.innerText || '').trim())
        .filter(value => value.length > 0 && value.length < 400);
    const blockedRequests = Array.from(
        performance.getEntriesByType('resource'),
        entry => entry.name,
    );
    return {
        title: document.title,
        text,
        rows: Array.from(new Set(rows)),
        resources: blockedRequests,
        statusText: document.body.innerText.match(/\d+\s*\/\s*\d+|\d+\s*blocked|\d+\s*not blocked/gi) || [],
    };
})()"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bs-adtest-", dir="/tmp/opencode",
                                     ignore_cleanup_errors=True) as temp_name:
        temp = Path(temp_name)
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
        report = {}
        with (ROOT / "release" / "chromium-adtest.log").open("w", encoding="utf-8") as log:
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

            # Let the extension arm itself before the test page loads.
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
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                added = worker.evaluate(
                    "(globalThis.__blueshieldTrackerDnrStats || {}).added || 0")
                if added >= 4900 and added == previous:
                    break
                previous = added
                time.sleep(1.5)
            time.sleep(2)
            worker.close()

            target_id = browser.send("Target.createTarget", {"url": "about:blank"})["targetId"]
            page = attach(port, target_id)
            page.send("Page.enable")
            page.send("Network.enable")
            page.send("Page.navigate", {"url": SITE})
            deadline = time.monotonic() + 60
            collected = None
            while time.monotonic() < deadline:
                time.sleep(2)
                try:
                    if page.evaluate("document.readyState", False) != "complete":
                        continue
                    collected = page.evaluate(COLLECT, False)
                except Exception:
                    continue
                if collected and re.search(r"\b(100|9\d)%|passed|blocked", collected["text"], re.I):
                    break
            time.sleep(5)
            collected = page.evaluate(COLLECT, False)

            blocked = [
                event["params"].get("request", {}).get("url", "")
                for event in page.events
                if event.get("method") == "Network.loadingFailed"
                and "BLOCKED" in str(event["params"].get("errorText", "")).upper()
            ]
            report["url"] = SITE
            report["verdictText"] = collected["text"][:4000]
            report["statusText"] = collected["statusText"]
            report["blockedRequests"] = sorted(set(blocked))
            report["blockedCount"] = len(set(blocked))
            report["rows"] = collected["rows"][:200]
            report["rowsAll"] = collected["rows"]
            page.close()
            report["ok"] = True
        except Exception as error:
            report["ok"] = False
            report["error"] = str(error)
            raise
        finally:
            if browser is not None:
                browser.close()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({k: v for k, v in report.items()
                      if k not in {"rowsAll", "verdictText"}}, indent=2)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
