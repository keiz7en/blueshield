#!/usr/bin/env python3
"""Profile BlueShield under a realistic, tracker-heavy browsing load.

Loads a real web page with the extension enabled and reports where the shared
service worker spends its time: DNR writes and reads, storage writes, content
script registration, per-request callbacks, and any long task that would show up
to the user as a stall.
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

REPORT = ROOT / "release" / "perf.json"
LOG = ROOT / "release" / "chromium-perf.log"

# A page that pulls in a wide mix of third-party resources, so both engines get
# exercised: static list matching, tracker learning, per-tab bookkeeping.
TARGETS = [
    "https://www.wikipedia.org/",
    "https://news.ycombinator.com/",
    "https://adblock.turtlecute.org/",
]
NAVIGATIONS = 2
# Chrome parks an idle MV3 worker after ~30s; wait past that so the next page
# load has to re-create it, the way it does during normal browsing.
IDLE_WAIT = 75


class CDP:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(url, timeout=20, origin="http://127.0.0.1")
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
            raise RuntimeError(json.dumps(result["exceptionDetails"])[:300])
        return result.get("result", {}).get("value")

    def close(self):
        self.ws.close()


def http_json(url):
    with urllib.request.urlopen(url, timeout=8) as response:
        return json.load(response)


def targets(port):
    return http_json(f"http://127.0.0.1:{port}/json/list")


def attach(port, target_id):
    target = next(item for item in targets(port) if item.get("id") == target_id)
    client = CDP(target["webSocketDebuggerUrl"])
    client.send("Runtime.enable")
    client.send("Log.enable")
    return client


PERF_SNAPSHOT = """(() => {
    const perf = globalThis.__blueshieldPerf;
    if (!perf) { return null; }
    const round = n => Math.round(n);
    return {
        dnrUpdate: {
            calls: perf.dnrUpdate.calls,
            rules: perf.dnrUpdate.rules,
            msTotal: round(perf.dnrUpdate.ms),
            msMax: round(perf.dnrUpdate.maxMs),
        },
        dnrNative: {
            calls: perf.dnrNative.calls,
            rules: perf.dnrNative.rules,
            msTotal: round(perf.dnrNative.ms),
            msMax: round(perf.dnrNative.maxMs),
        },
        dnrRead: {
            calls: perf.dnrRead.calls,
            msTotal: round(perf.dnrRead.ms),
            msMax: round(perf.dnrRead.maxMs),
        },
        storageSet: {
            calls: perf.storageSet.calls,
            kbTotal: round(perf.storageSet.rules / 1024),
            msTotal: round(perf.storageSet.ms),
            msMax: round(perf.storageSet.maxMs),
        },
        storageGet: {
            calls: perf.storageGet.calls,
            msTotal: round(perf.storageGet.ms),
            msMax: round(perf.storageGet.maxMs),
        },
        scripting: {
            calls: perf.scripting.calls,
            msTotal: round(perf.scripting.ms),
            msMax: round(perf.scripting.maxMs),
        },
        requests: {
            count: perf.requests.count,
            msTotal: round(perf.requests.ms),
            msMax: round(perf.requests.maxMs),
        },
        longTasks: perf.longTasks.map(t => ({ at: t.at, ms: t.ms })),
    };
})()"""


def delta(after, before):
    out = {}
    for key, value in after.items():
        if key == "longTasks" or not isinstance(value, dict):
            continue
        out[key] = {}
        for field, current in value.items():
            previous = (before.get(key) or {}).get(field)
            if isinstance(current, (int, float)) and isinstance(previous, (int, float)):
                out[key][field] = current - previous
            else:
                out[key][field] = current
    out["longTasks"] = after["longTasks"]
    return out


def main() -> int:
    if not STAGE.is_dir():
        raise RuntimeError(f"Missing unpacked extension: {STAGE}")

    with tempfile.TemporaryDirectory(prefix="bs-perf-", dir="/tmp/opencode",
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
        with LOG.open("w", encoding="utf-8") as log:
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
            deadline = time.monotonic() + 30
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
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if worker.evaluate("(async () => Boolean(globalThis.badger && badger.INITIALIZED))()"):
                    break
                time.sleep(0.5)
            time.sleep(5)

            startup = worker.evaluate(PERF_SNAPSHOT)
            if startup is None:
                raise RuntimeError("performance counters are missing from the worker")
            report["startup"] = startup
            before = worker.evaluate(PERF_SNAPSHOT)

            # Real browsing load.
            visited = []
            for _ in range(NAVIGATIONS):
                for url in TARGETS:
                    target_id = browser.send("Target.createTarget", {"url": "about:blank"})["targetId"]
                    page = attach(port, target_id)
                    page.send("Page.enable")
                    page.send("Network.enable")
                    started = time.monotonic()
                    page.send("Page.navigate", {"url": url})
                    load_deadline = time.monotonic() + 30
                    while time.monotonic() < load_deadline:
                        time.sleep(0.5)
                        try:
                            if page.evaluate("document.readyState", False) == "complete":
                                break
                        except Exception:
                            break
                    wall = time.monotonic() - started
                    requests = sum(
                        1 for event in page.events
                        if event.get("method") == "Network.requestWillBeSent"
                    )
                    blocked = sum(
                        1 for event in page.events
                        if event.get("method") == "Network.loadingFailed"
                        and "BLOCKED" in str(event["params"].get("errorText", "")).upper()
                    )
                    visited.append({
                        "url": url,
                        "wallSeconds": round(wall, 2),
                        "requests": requests,
                        "blocked": blocked,
                    })
                    time.sleep(1.5)
                    page.close()
                    browser.send("Target.closeTarget", {"targetId": target_id})

            time.sleep(4)
            after = worker.evaluate(PERF_SNAPSHOT)
            report["browsing"] = delta(after, before)
            report["pages"] = visited
            report["totalPages"] = sum(item["requests"] for item in visited)
            report["totalBlocked"] = sum(item["blocked"] for item in visited)

            # ---------------------------------------------------------------- #
            # The decisive experiment: an MV3 service worker is killed after a
            # short idle period and re-created on the next event. If the whole
            # rule set is installed again every time, the user gets a
            # multi-second stall every few minutes of ordinary browsing.
            # ---------------------------------------------------------------- #
            warm_before = worker.evaluate(PERF_SNAPSHOT)
            report["warmWaitSeconds"] = IDLE_WAIT
            time.sleep(IDLE_WAIT)
            wake_target = browser.send("Target.createTarget", {"url": "about:blank"})["targetId"]
            wake_page = attach(port, wake_target)
            wake_page.send("Page.enable")
            warm_started = time.monotonic()
            wake_page.send("Page.navigate", {"url": TARGETS[0]})
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                time.sleep(0.5)
                try:
                    if wake_page.evaluate("document.readyState", False) == "complete":
                        break
                except Exception:
                    break
            warm_wall = time.monotonic() - warm_started
            time.sleep(6)
            warm_after = worker.evaluate(PERF_SNAPSHOT)
            report["warmWake"] = delta(warm_after, warm_before)
            report["warmWake"]["pageWallSeconds"] = round(warm_wall, 2)
            wake_page.close()
            browser.send("Target.closeTarget", {"targetId": wake_target})

            worst = report["browsing"]
            stalls = [task for task in worst["longTasks"] if task["ms"] >= 500]
            report["stalls500ms"] = stalls
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

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
