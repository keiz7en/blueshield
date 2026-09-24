#!/usr/bin/env python3
"""Measure BlueShield's per-tab cost and prove inactive tabs are cleaned up.

Opens N tabs against a local server, lets the tracker engine learn something on
each, then:
  * measures the service-worker heap, DNR rule counts, the tracker's per-tab
    data and session storage at 1 tab and at N tabs;
  * switches between a background tab and a fresh tab to time re-activation;
  * closes every tab and verifies everything returns to the baseline, i.e. the
    extension does not accumulate state per tab.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import websocket

from project_paths import ROOT, STAGE

REPORT = ROOT / "release" / "tab-memory.json"
LOG = ROOT / "release" / "chromium-tabs.log"
TABS = 12

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>tab {n}</title></head>
<body><h1>tab {n}</h1>
<img src="https://www.google-analytics.com/collect?tab={n}" alt="">
<img src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?tab={n}" alt="">
<img src="/pixel.gif" alt="">
</body></html>
"""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class CDP:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(url, timeout=15, origin="http://127.0.0.1")
        self.next_id = 1
        self.events = []

    def send(self, method, params=None, timeout=30):
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
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        }, timeout=60)
        details = result.get("exceptionDetails")
        if details:
            raise RuntimeError(details.get("text", "failed") + ": " + json.dumps(details))
        return result.get("result", {}).get("value")

    def heap(self):
        return int(self.send("Runtime.getHeapUsage").get("usedSize", 0))

    def close(self):
        self.ws.close()


def http_json(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def targets(port):
    return http_json(f"http://127.0.0.1:{port}/json/list")


def attach(port, target_id):
    target = next(item for item in targets(port) if item.get("id") == target_id)
    client = CDP(target["webSocketDebuggerUrl"])
    client.send("Runtime.enable")
    return client


def wait_devtools(profile, process, timeout=20):
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Chromium exited early: {process.returncode}")
        if port_file.exists() and port_file.read_text().splitlines():
            return int(port_file.read_text().splitlines()[0])
        time.sleep(0.1)
    raise TimeoutError("devtools endpoint")


STATE = r'''(async () => {
    const [dynamic, session] = await Promise.all([
        chrome.declarativeNetRequest.getDynamicRules(),
        chrome.declarativeNetRequest.getSessionRules(),
    ]);
    const local = await chrome.storage.local.get(null);
    const sessionStore = await chrome.storage.session.get(null);
    const tabs = await chrome.tabs.query({});
    const badger = globalThis.badger;
    const tabData = badger && badger.tabData ? badger.tabData._tabData : {};
    const trackerTabs = Object.keys(tabData);
    let trackerBytes = 0;
    for (const id of trackerTabs) {
        trackerBytes += JSON.stringify(tabData[id] || {}).length;
    }
    const perTabSessionRules = session.filter(r => Array.isArray(r.condition?.tabIds)).length;
    return {
        openTabs: tabs.length,
        dynamicRules: dynamic.length,
        sessionRules: session.length,
        perTabSessionRules,
        trackerTabCount: trackerTabs.length,
        trackerTabBytes: trackerBytes,
        localKeys: Object.keys(local).length,
        localBytes: await chrome.storage.local.getBytesInUse(null),
        sessionKeys: Object.keys(sessionStore).length,
        sessionBytes: await chrome.storage.session.getBytesInUse(null),
    };
})()'''


def main() -> int:
    if not STAGE.is_dir():
        raise RuntimeError(f"Missing unpacked extension: {STAGE}")

    with tempfile.TemporaryDirectory(prefix="bs-tabs-", dir="/tmp/opencode",
                                     ignore_cleanup_errors=True) as temp_name:
        temp = Path(temp_name)
        site = temp / "site"
        site.mkdir()
        for index in range(TABS):
            (site / f"tab{index}.html").write_text(PAGE.format(n=index), encoding="utf-8")
        (site / "pixel.gif").write_bytes(b"GIF89a\x01\x00\x01\x00\x00\x00\x00;")

        handler = functools.partial(QuietHandler, directory=str(site))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        profile = temp / "profile"
        for name in ("home", "config", "cache"):
            (temp / name).mkdir()
        env = dict(os.environ, HOME=str(temp / "home"),
                   XDG_CONFIG_HOME=str(temp / "config"),
                   XDG_CACHE_HOME=str(temp / "cache"))
        command = [
            "chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
            "--disable-background-networking", "--no-first-run",
            f"--user-data-dir={profile}", "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            f"--disable-extensions-except={STAGE}", f"--load-extension={STAGE}",
            "--enable-logging=stderr", "--v=0", "about:blank",
        ]
        report = {}
        with LOG.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        browser = None
        try:
            devtools = wait_devtools(profile, process)
            browser = CDP(http_json(f"http://127.0.0.1:{devtools}/json/version")["webSocketDebuggerUrl"])
            browser.send("Target.setDiscoverTargets", {"discover": True})

            worker_target = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and worker_target is None:
                for target in targets(devtools):
                    if target.get("type") == "service_worker" and \
                            "blueshield-service-worker.js" in target.get("url", ""):
                        worker_target = target
                        break
                time.sleep(0.25)
            if worker_target is None:
                raise RuntimeError("service worker target not found")
            extension_id = worker_target["url"].split("/")[2]

            worker = attach(devtools, worker_target["id"])
            worker.send("Runtime.runIfWaitingForDebugger")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                ready = worker.evaluate(
                    "(async () => Boolean(globalThis.badger && badger.INITIALIZED))()")
                if ready:
                    break
                time.sleep(0.5)
            time.sleep(3)

            baseline = worker.evaluate(STATE)
            baseline["workerHeapBytes"] = worker.heap()
            report["baseline_one_tab"] = baseline

            opened = []
            for index in range(TABS):
                url = f"http://127.0.0.1:{port}/tab{index}.html"
                target_id = browser.send("Target.createTarget", {"url": url})["targetId"]
                opened.append(target_id)
                page = attach(devtools, target_id)
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if page.evaluate("document.readyState", False) in {"interactive", "complete"}:
                        break
                    time.sleep(0.2)
                time.sleep(0.7)
                page.close()

            # Let every tab finish reporting its trackers.
            time.sleep(6)
            loaded = worker.evaluate(STATE)
            loaded["workerHeapBytes"] = worker.heap()
            report["with_tabs"] = loaded

            # Time re-activation of a tab that has been in the background.
            first = opened[0]
            browser.send("Target.activateTarget", {"targetId": first})
            time.sleep(2)
            page = attach(devtools, first)
            started = time.monotonic()
            page.evaluate("document.title")
            reactivate_ms = round((time.monotonic() - started) * 1000, 1)
            blocked_here = [
                event["params"].get("request", {}).get("url", "")
                for event in page.events
                if event.get("method") == "Network.loadingFailed"
            ]
            page.close()
            report["reactivation"] = {
                "scriptRoundTripMs": reactivate_ms,
                "stillInteractive": True,
            }

            for target_id in opened:
                try:
                    browser.send("Target.closeTarget", {"targetId": target_id})
                except Exception:
                    pass
            time.sleep(8)  # tab cleanup runs 2s after close, plus settling

            after = worker.evaluate(STATE)
            after["workerHeapBytes"] = worker.heap()
            report["after_close"] = after

            per_tab = {
                "tabs": loaded["openTabs"] - baseline["openTabs"],
                "workerHeapBytesPerTab": round(
                    (loaded["workerHeapBytes"] - baseline["workerHeapBytes"])
                    / max(1, loaded["openTabs"] - baseline["openTabs"]) / 1024, 1),
                "trackerBytesPerTab": round(
                    (loaded["trackerTabBytes"] - baseline["trackerTabBytes"])
                    / max(1, loaded["openTabs"] - baseline["openTabs"]), 1),
                "dynamicRuleGrowth": loaded["dynamicRules"] - baseline["dynamicRules"],
                "sessionRuleGrowth": loaded["sessionRules"] - baseline["sessionRules"],
            }
            report["per_tab"] = per_tab

            problems = []
            if after["trackerTabCount"] > baseline["trackerTabCount"]:
                problems.append(
                    f"tracker data leaked: {after['trackerTabCount']} tab entries remain "
                    f"(baseline {baseline['trackerTabCount']})")
            if after["dynamicRules"] > baseline["dynamicRules"] + 5:
                problems.append(
                    f"dynamic rules grew by {after['dynamicRules'] - baseline['dynamicRules']} after closing tabs")
            if after["sessionRules"] > baseline["sessionRules"] + 5:
                problems.append(
                    f"session rules grew by {after['sessionRules'] - baseline['sessionRules']} after closing tabs")
            if after["localBytes"] > baseline["localBytes"] + 200_000:
                problems.append("extension storage grew by more than 200 KB after closing tabs")
            if per_tab["workerHeapBytesPerTab"] > 1024:
                problems.append(
                    f"worker heap grew {per_tab['workerHeapBytesPerTab']} KB per tab")
            if reactivate_ms > 500:
                problems.append(f"re-activating a background tab took {reactivate_ms} ms")
            report["problems"] = problems
            report["ok"] = not problems
        except Exception as error:
            report["ok"] = False
            report["error"] = str(error)
            raise
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

    print(json.dumps(report, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
