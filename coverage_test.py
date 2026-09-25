#!/usr/bin/env python3
"""Measure BlueShield's coverage against the TurtleCute AdBlockTest host list.

The dataset is the same one the public test site uses: known ad, analytics,
error-reporting, social, mixed and OEM-telemetry hosts. Every host is requested
from a page in the browser, and the request is judged blocked or not from the
network layer, so a DNS failure can never be mistaken for a block.
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
from collections import defaultdict
from pathlib import Path

import websocket

from project_paths import ROOT, STAGE

REPORT = ROOT / "release" / "coverage.json"
DATASET = Path("/tmp/opencode/adblock_data.json")
DATASET_URL = "https://raw.githubusercontent.com/Turtlecute33/adblocktest/master/src/data/adblock_data.json"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>coverage</title></head>
<body>
<!-- Selectors taken from the generic cosmetic lists the extension ships, so
     this measures real element hiding rather than an arbitrary guess. -->
<div class="inplayer-ad" style="height:40px">in-player ad</div>
<div class="advboxemb" style="height:40px">ad box</div>
<div class="bottom-hor-block" style="height:40px">bottom block</div>
<div id="keep-me" style="height:40px">content</div>
<script>
window.__done = false;
const hosts = %s;
let remaining = hosts.length;
const results = {};
for (const host of hosts) {
    const img = new Image();
    img.src = 'https://' + host + '/__coverage_probe__?' + Math.random();
    let done = false;
    const finish = (blocked) => {
        if (done) { return; }
        done = true;
        results[host] = blocked;
        remaining -= 1;
        if (remaining === 0) { window.__results = results; window.__done = true; }
    };
    img.onload = () => finish(false);
    img.onerror = () => finish(true);
    setTimeout(() => finish(false), 12000);
}
setTimeout(() => { window.__results = results; window.__done = true; }, 30000);
</script>
</body></html>
"""


COSMETIC_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>cosmetic</title></head>
<body>
<!-- Selectors taken from the generic cosmetic lists the extension ships, so
     this measures real element hiding rather than an arbitrary guess. -->
<div class="inplayer-ad" style="height:40px">in-player ad</div>
<div class="advboxemb" style="height:40px">ad box</div>
<div class="bottom-hor-block" style="height:40px">bottom block</div>
<div id="keep-me" style="height:40px">content</div>
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
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def targets(port):
    return http_json(f"http://127.0.0.1:{port}/json/list")


def attach(port, target_id):
    target = next(item for item in targets(port) if item.get("id") == target_id)
    client = CDP(target["webSocketDebuggerUrl"])
    client.send("Runtime.enable")
    return client


def load_dataset() -> dict:
    if not DATASET.is_file():
        with urllib.request.urlopen(DATASET_URL, timeout=30) as response:
            DATASET.write_bytes(response.read())
    return json.loads(DATASET.read_text(encoding="utf-8"))


def main() -> int:
    dataset = load_dataset()
    groups = {}
    for category, entries in dataset.items():
        if isinstance(entries, dict):
            for name, hosts in entries.items():
                if isinstance(hosts, list):
                    groups[f"{category}/{name}"] = hosts
    hosts = sorted({host for values in groups.values() for host in values if isinstance(host, str)})

    with tempfile.TemporaryDirectory(prefix="bs-cov-", dir="/tmp/opencode",
                                     ignore_cleanup_errors=True) as temp_name:
        temp = Path(temp_name)
        site = temp / "site"
        site.mkdir()
        (site / "index.html").write_text(PAGE % json.dumps(hosts), encoding="utf-8")
        (site / "cosmetic.html").write_text(COSMETIC_PAGE, encoding="utf-8")
        handler = functools.partial(QuietHandler, directory=str(site))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
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
            "--v=0", "about:blank",
        ]
        report = {}
        with (ROOT / "release" / "chromium-coverage.log").open("w", encoding="utf-8") as log:
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
            # Wait for the network rules *and* the cosmetic content scripts.
            # Content scripts registered after a document loads never run in it,
            # so the test page has to be opened only once both are in place.
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
            time.sleep(2)
            worker.close()

            # Attach to a blank tab first: navigating before Network.enable
            # would miss the events for requests that are blocked instantly.
            target_id = browser.send("Target.createTarget", {"url": "about:blank"})["targetId"]
            page = attach(port, target_id)
            page.send("Page.enable")
            page.send("Network.enable")
            page.send("Page.navigate", {"url": f"http://127.0.0.1:{site_port}/index.html"})

            results = {}
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if page.evaluate("window.__done === true", False):
                    results = page.evaluate("window.__results", False) or {}
                    break
                time.sleep(2)
            time.sleep(2)
            results = page.evaluate("window.__results", False) or {}

            # The network log is the authority: a request that never reached the
            # network was stopped by the extension, not by a DNS failure.
            # loadingFailed carries only a requestId, so map it back through the
            # request log to learn which host was stopped.
            urls_by_id = {
                event["params"]["requestId"]: event["params"]["request"]["url"]
                for event in page.events
                if event.get("method") == "Network.requestWillBeSent"
            }
            blocked = {
                urls_by_id.get(event["params"].get("requestId"), "")
                for event in page.events
                if event.get("method") == "Network.loadingFailed"
                and "BLOCKED" in str(event["params"].get("errorText", "")).upper()
            }
            blocked_hosts = set()
            for url in blocked:
                for host in hosts:
                    if host in url:
                        blocked_hosts.add(host)

            # A probe can be dropped by the network stack without the extension
            # being involved, so anything not seen as blocked is retried once in
            # a fresh document before it is reported as a genuine gap.
            unconfirmed = [host for host in hosts if host not in blocked_hosts]
            retried = []
            for attempt in range(2):
                if not unconfirmed:
                    break
                (site / "retry.html").write_text(
                    PAGE % json.dumps(unconfirmed), encoding="utf-8")
                page.events.clear()
                page.send("Page.navigate", {
                    "url": f"http://127.0.0.1:{site_port}/retry.html",
                })
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    time.sleep(2)
                    try:
                        if page.evaluate("window.__done === true", False):
                            break
                    except Exception:
                        break
                time.sleep(2)
                retry_urls = {
                    event["params"]["requestId"]: event["params"]["request"]["url"]
                    for event in page.events
                    if event.get("method") == "Network.requestWillBeSent"
                }
                retry_blocked = {
                    retry_urls.get(event["params"].get("requestId"), "")
                    for event in page.events
                    if event.get("method") == "Network.loadingFailed"
                    and "BLOCKED" in str(event["params"].get("errorText", "")).upper()
                }
                confirmed = set()
                for url in retry_blocked:
                    for host in unconfirmed:
                        if host in url:
                            confirmed.add(host)
                blocked_hosts |= confirmed
                retried.append({
                    "attempt": attempt + 1,
                    "requested": len(unconfirmed),
                    "confirmedBlocked": sorted(confirmed),
                })
                unconfirmed = [host for host in unconfirmed if host not in confirmed]
            report["retries"] = retried

            per_group = defaultdict(lambda: {"total": 0, "blocked": 0, "missed": []})
            for group, entries in groups.items():
                for host in entries:
                    if host not in hosts:
                        continue
                    per_group[group]["total"] += 1
                    if host in blocked_hosts:
                        per_group[group]["blocked"] += 1
                    else:
                        per_group[group]["missed"].append(host)

            seen = [
                event["params"]["request"]["url"]
                for event in page.events
                if event.get("method") == "Network.requestWillBeSent"
            ]
            failures = [
                (event["params"].get("request", {}).get("url", ""),
                 event["params"].get("errorText", ""))
                for event in page.events
                if event.get("method") == "Network.loadingFailed"
            ]
            # Cosmetic filtering is judged on a fresh, plain document: the host
            # probe page above keeps creating nodes for a minute, which is not
            # how a normal page behaves.
            page.send("Page.navigate", {"url": f"http://127.0.0.1:{site_port}/cosmetic.html"})
            time.sleep(8)
            cosmetic = page.evaluate("""(() => {
                const visible = sel => {
                    const el = document.querySelector(sel);
                    if (!el) { return null; }
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && rect.width > 0 && rect.height > 0;
                };
                return {
                    inplayerAdHidden: visible('.inplayer-ad') === false,
                    advboxembHidden: visible('.advboxemb') === false,
                    bottomBlockHidden: visible('.bottom-hor-block') === false,
                    contentStillVisible: visible('#keep-me'),
                };
            })()""", False)
            report["cosmetic"] = cosmetic

            report["cosmeticStyleState"] = page.evaluate("""(() => {
                const rules = [];
                for (const sheet of document.styleSheets) {
                    try {
                        for (const rule of sheet.cssRules) { rules.push(rule.cssText); }
                    } catch (e) { /* cross-origin */ }
                }
                return {
                    sheets: document.styleSheets.length,
                    adopted: document.adoptedStyleSheets.length,
                    sample: rules.slice(0, 6),
                    mentionsInplayer: rules.some(r => r.includes('inplayer-ad')),
                };
            })()""", False)


            report["diagnostics"] = {
                "requestsSeen": len(seen),
                "loadingFailed": len(failures),
                "sampleFailures": failures[:10],
                "pageReported": len(results),
                "pageBlockedCount": sum(1 for value in results.values() if value),
                "sampleRequests": seen[:5],
            }
            report["totalHosts"] = len(hosts)
            report["blockedHosts"] = len(blocked_hosts)
            report["percent"] = round(100 * len(blocked_hosts) / max(1, len(hosts)), 1)
            report["groups"] = {group: value for group, value in sorted(per_group.items())}
            report["missedHosts"] = sorted(set(hosts) - blocked_hosts)
            cosmetic = report.get("cosmetic") or {}
            report["cosmeticPassed"] = bool(
                cosmetic.get("inplayerAdHidden")
                and cosmetic.get("advboxembHidden")
                and cosmetic.get("bottomBlockHidden")
                and cosmetic.get("contentStillVisible")
            )
            report["passed"] = not report["missedHosts"] and report["cosmeticPassed"]
            page.close()
            report["ok"] = True
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

    print(f"blocked {report['blockedHosts']}/{report['totalHosts']} hosts ({report['percent']}%)")
    for group, value in report["groups"].items():
        flag = "OK " if not value["missed"] else "GAP"
        print(f"  {flag} {group}: {value['blocked']}/{value['total']}"
              + (f"  missing: {', '.join(value['missed'][:6])}" if value["missed"] else ""))
    print("cosmetic element hiding:", "PASS" if report["cosmeticPassed"] else "FAIL",
          report.get("cosmetic"))
    if report["missedHosts"]:
        print("unblocked hosts:", ", ".join(report["missedHosts"]))
    print("RESULT:", "PASS" if report["passed"] else "FAIL")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
