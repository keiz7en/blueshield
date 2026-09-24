#!/usr/bin/env python3
"""Load BlueShield in Chromium and verify blocking, storage, theme and memory."""

from __future__ import annotations

import functools
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import websocket

from project_paths import ROOT, STAGE, VERSION

REPORT = ROOT / "release" / "smoke-test.json"
LOG = ROOT / "release" / "chromium-smoke.log"
EXPECTED_ID = "jbdmpgnpkidpmibioddiapfgklfncgcf"
TRACKER_VENDOR = "vendor/tracker-protection"

# Requests the bundled block lists are expected to stop. The first is a classic
# advertising host, the second a well-known analytics tracker.
AD_URL = "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"
TRACKER_URL = "https://www.google-analytics.com/collect?v=1"

TEST_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>BlueShield test page</title></head>
<body>
<h1>BlueShield blocking test</h1>
<img src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js" alt="">
<img src="https://www.google-analytics.com/collect?v=1" alt="">
<img src="http://127.0.0.1:{port}/pixel.gif" alt="">
</body></html>
"""


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 - silence the test server
        pass


def start_test_server(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = functools.partial(QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


class CDP:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(
            url,
            timeout=10,
            origin="http://127.0.0.1",
            suppress_origin=False,
        )
        self.next_id = 1
        self.events = []

    def send(self, method: str, params=None, timeout=30):
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

    def evaluate(self, expression: str, await_promise: bool = True):
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
                "userGesture": True,
            },
            timeout=60,
        )
        details = result.get("exceptionDetails")
        if details:
            raise RuntimeError(details.get("text", "Evaluation failed") + ": " + json.dumps(details))
        return result.get("result", {}).get("value")

    def heap(self) -> int:
        """Used JS heap size in bytes."""
        usage = self.send("Runtime.getHeapUsage")
        return int(usage.get("usedSize", 0))

    def close(self):
        self.ws.close()


def http_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def wait_for_devtools(profile: Path, process: subprocess.Popen[bytes], timeout=20):
    port_file = profile / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Chromium exited early with {process.returncode}")
        if port_file.exists():
            text = port_file.read_text().splitlines()
            if text:
                return int(text[0])
        time.sleep(0.1)
    raise TimeoutError("Chromium DevTools endpoint did not start")


def target_list(port: int):
    return http_json(f"http://127.0.0.1:{port}/json/list")


def find_target(port: int, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    last = []
    while time.monotonic() < deadline:
        last = target_list(port)
        for target in last:
            if predicate(target):
                return target
        time.sleep(0.25)
    raise TimeoutError(f"Target not found; saw {[(t.get('type'), t.url) for t in last]}")


def attach(port: int, target_id: str) -> CDP:
    target = next(item for item in target_list(port) if item.get("id") == target_id)
    client = CDP(target["webSocketDebuggerUrl"])
    client.send("Runtime.enable")
    client.send("Log.enable")
    return client


def open_page(browser: CDP, url: str):
    return browser.send("Target.createTarget", {"url": url})["targetId"]


def close_page(browser: CDP, target_id: str):
    try:
        browser.send("Target.closeTarget", {"targetId": target_id})
    except Exception:
        pass


def open_and_attach(browser: CDP, port: int, url: str, ready_check: str, timeout=20):
    """Open a page and return an attached client once it reports ready."""
    target_id = open_page(browser, url)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                target = find_target(
                    port,
                    lambda item, tid=target_id: item.get("id") == tid,
                    timeout=2,
                )
                client = attach(port, target["id"])
                client.send("Runtime.runIfWaitingForDebugger")
                if client.evaluate("document.readyState", False) in {"interactive", "complete"}:
                    if client.evaluate(ready_check, False):
                        return client
                client.close()
            except Exception:
                pass
            time.sleep(0.2)
        raise RuntimeError(f"Page never became ready: {url}")
    except Exception:
        close_page(browser, target_id)
        raise


FORBIDDEN_TEXT = re.compile(r"uBlock|Privacy\s*Badger|\bbadger\b|\bEFF\b|eff\.org", re.IGNORECASE)


def page_scan(client: CDP) -> dict:
    """Collect the visible text and theme state of a page."""
    return client.evaluate(
        """(() => {
            const text = document.body ? document.body.innerText : '';
            const root = getComputedStyle(document.documentElement);
            const bodyStyle = document.body ? getComputedStyle(document.body) : null;
            return {
                title: document.title,
                text,
                primary: root.getPropertyValue('--primary-40').trim(),
                colorScheme: root.colorScheme,
                bodyBackground: bodyStyle ? bodyStyle.backgroundImage + '|' + bodyStyle.backgroundColor : '',
                bodyColor: bodyStyle ? bodyStyle.color : '',
                lightClass: document.documentElement.classList.contains('light'),
                brandingImages: Array.from(document.images)
                    .filter(img => img.src.includes('/branding/'))
                    .map(img => ({ src: img.src.split('/').pop(), loaded: img.complete && img.naturalWidth > 0 })),
                visibleTextLength: (document.body.innerText || '').trim().length
            };
        })()""",
        False,
    )


def assert_clean_ui(name: str, page: dict) -> None:
    found = FORBIDDEN_TEXT.search(page.get("text") or "")
    if found:
        raise AssertionError(
            f"{name} shows upstream branding: …{(page.get('text') or '')[max(0, found.start() - 60):found.end() + 60]}…"
        )
    if not page.get("title", "").startswith("BlueShield") and name != "dashboard":
        raise AssertionError(f"{name} has an unexpected title: {page.get('title')!r}")
    for image in page.get("brandingImages", []):
        if not image["loaded"]:
            raise AssertionError(f"{name} failed to load its icon: {image['src']}")
    if page.get("visibleTextLength", 0) < 20:
        raise AssertionError(
            f"{name} rendered no visible text (a stylesheet may be hiding the page): "
            f"{page.get('bodyBackground')!r}"
        )


def main() -> int:
    if not STAGE.is_dir():
        raise RuntimeError(f"Missing unpacked extension: {STAGE}")

    with tempfile.TemporaryDirectory(
        prefix="blueshield-smoke-", dir="/tmp/opencode", ignore_cleanup_errors=True
    ) as temp_name:
        temp = Path(temp_name)
        site = temp / "site"
        site.mkdir()
        profile = temp / "profile"
        home = temp / "home"
        config = temp / "config"
        cache = temp / "cache"
        for path in (profile, home, config, cache):
            path.mkdir()

        server, site_port = start_test_server(site)
        (site / "index.html").write_text(TEST_PAGE.format(port=site_port), encoding="utf-8")
        (site / "pixel.gif").write_bytes(b"GIF89a\x01\x00\x01\x00\x00\x00\x00;")

        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
        })
        command = [
            "chromium",
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-background-networking",
            "--no-first-run",
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            f"--disable-extensions-except={STAGE}",
            f"--load-extension={STAGE}",
            "--enable-logging=stderr",
            "--v=0",
            "about:blank",
        ]
        with LOG.open("w", encoding="utf-8") as log_handle:
            process = subprocess.Popen(
                command,
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        browser = None
        report = {}
        try:
            port = wait_for_devtools(profile, process)
            browser = CDP(http_json(f"http://127.0.0.1:{port}/json/version")["webSocketDebuggerUrl"])
            browser.send("Target.setDiscoverTargets", {"discover": True})

            worker_target = find_target(
                port,
                lambda target: target.get("type") == "service_worker"
                and target.get("url", "").endswith("/blueshield-service-worker.js"),
                timeout=30,
            )
            extension_id = worker_target["url"].split("/")[2]
            if extension_id != EXPECTED_ID:
                raise AssertionError(f"Unexpected extension ID: {extension_id}")

            worker = None
            worker_attach_error = None
            worker_deadline = time.monotonic() + 30
            while time.monotonic() < worker_deadline:
                try:
                    live_target = find_target(
                        port,
                        lambda target: target.get("type") == "service_worker"
                        and target.get("url", "").endswith("/blueshield-service-worker.js"),
                        timeout=2,
                    )
                    candidate = attach(port, live_target["id"])
                    candidate.send("Runtime.runIfWaitingForDebugger")
                    if candidate.evaluate("typeof globalThis.chrome", False) == "object":
                        worker = candidate
                        break
                    candidate.close()
                except Exception as error:
                    worker_attach_error = error
                time.sleep(0.25)
            if worker is None:
                raise RuntimeError(
                    "Could not attach to the live extension service worker: "
                    + str(worker_attach_error)
                )

            state_expression = r'''(async () => {
                const dynamicRules = await chrome.declarativeNetRequest.getDynamicRules();
                const sessionRules = await chrome.declarativeNetRequest.getSessionRules();
                const enabledRulesets = await chrome.declarativeNetRequest.getEnabledRulesets();
                const registered = await chrome.scripting.getRegisteredContentScripts();
                const buildInfo = await fetch(
                    chrome.runtime.getURL('BUILD_INFO.json')
                ).then(response => response.json());
                const local = await chrome.storage.local.get(null);
                const session = await chrome.storage.session.get(null);
                return {
                    manifest: {
                        name: chrome.runtime.getManifest().name,
                        version: chrome.runtime.getManifest().version,
                        manifestVersion: chrome.runtime.getManifest().manifest_version,
                        optionsPage: (chrome.runtime.getManifest().options_ui || {}).page
                    },
                    trackerEngine: {
                        exists: Boolean(globalThis.badger),
                        initialized: Boolean(globalThis.badger && globalThis.badger.INITIALIZED),
                        criticalError: globalThis.badger && globalThis.badger.criticalError,
                        version: buildInfo.components.trackerProtection.version
                    },
                    blockingEngine: {
                        version: buildInfo.components.blockingEngine.version
                    },
                    dnr: {
                        dynamicCount: dynamicRules.length,
                        trackerDynamicCount: dynamicRules.filter(r =>
                            r.id >= 100000000 && r.id < 120000000
                        ).length,
                        sessionCount: sessionRules.length,
                        trackerSessionCount: sessionRules.filter(r =>
                            r.id >= 120000000 && r.id < 140000000
                        ).length,
                        blockingSessionRuleCount: sessionRules.filter(r =>
                            r.id >= 1000000 && r.id < 3000000
                        ).length,
                        duplicateDynamicIds: dynamicRules.length - new Set(
                            dynamicRules.map(r => r.id)
                        ).size,
                        enabledStaticRulesets: enabledRulesets.length,
                        hasTrackerDntRuleset: enabledRulesets.includes('dnt_policy_ruleset'),
                        hasTrackerIgnoreRuleset: enabledRulesets.includes('ignore_ruleset')
                    },
                    trackerDnrStats: {
                        ...(globalThis.__blueshieldTrackerDnrStats || {})
                    },
                    storage: {
                        localKeys: Object.keys(local).sort(),
                        trackerLocalKeys: Object.keys(local).filter(k =>
                            k.startsWith('privacybadger.')
                        ).sort(),
                        trackerSessionKeys: Object.keys(session).filter(k =>
                            k.startsWith('privacybadger.')
                        ).sort(),
                        rulesetConfig: local.rulesetConfig || null
                    },
                    contentScripts: registered.map(s => s.id).sort()
                };
            })()'''
            state = None
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                state = worker.evaluate(state_expression)
                stats = state.get("trackerDnrStats", {})
                if state["trackerEngine"]["initialized"] and stats.get("skipped", 0) == 0:
                    # The tracker engine can be briefly blocked by the other
                    # engine's rules while they are rewritten; wait for the
                    # retries to settle before judging completeness.
                    if state["dnr"]["trackerDynamicCount"] >= 4900:
                        break
                time.sleep(0.5)
            report["serviceWorker"] = state
            report["workerEvents"] = [
                event for event in worker.events
                if event.get("method") in {"Runtime.exceptionThrown", "Log.entryAdded"}
            ]

            if not state or not state["trackerEngine"]["exists"]:
                raise AssertionError("Tracker engine singleton did not initialize")
            if not state["trackerEngine"]["initialized"]:
                raise AssertionError("Tracker engine did not finish initialization")
            if state["trackerEngine"].get("criticalError"):
                raise AssertionError(state["trackerEngine"]["criticalError"])
            if state["manifest"]["name"] != "BlueShield":
                raise AssertionError(f"Unexpected extension name: {state['manifest']['name']}")
            if state["manifest"]["optionsPage"] != "blueshield-settings.html":
                raise AssertionError("Unified settings page is not the options page")
            if state["dnr"]["duplicateDynamicIds"]:
                raise AssertionError("Duplicate dynamic DNR IDs detected")
            if state["dnr"]["trackerDynamicCount"] < 4000:
                raise AssertionError(
                    "Learned tracker rules were truncated: "
                    + str(state["dnr"]["trackerDynamicCount"])
                )
            if state["dnr"]["blockingSessionRuleCount"] <= 0:
                raise AssertionError("Blocking engine regex rules are missing")
            if state.get("trackerDnrStats", {}).get("skipped", 0) != 0:
                raise AssertionError(
                    "Invalid tracker DNR rules were skipped: "
                    + json.dumps(state["trackerDnrStats"])
                )
            if not state["dnr"]["hasTrackerDntRuleset"] or not state["dnr"]["hasTrackerIgnoreRuleset"]:
                raise AssertionError("Tracker static DNR rulesets are not enabled")
            if not state["storage"]["trackerLocalKeys"]:
                raise AssertionError("Tracker storage namespace was not created")
            if "privacybadger.dnt_signal" not in state["contentScripts"]:
                raise AssertionError("Tracker Do Not Track content script was not registered")

            # ---------------------------------------------------------------- #
            # End-to-end blocking: a real page load must lose both the ad and
            # the tracker request at the network layer. The failures are read
            # from the browser's own network log, so no extra permission and no
            # cooperation from the page is needed.
            # ---------------------------------------------------------------- #
            test_url = f"http://127.0.0.1:{site_port}/index.html"
            page_target_id = open_page(browser, "about:blank")
            page = None
            try:
                page = attach(port, page_target_id)
                page.send("Runtime.runIfWaitingForDebugger")
                page.send("Page.enable")
                page.send("Network.enable")
                page.send("Page.navigate", {"url": test_url})
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    time.sleep(1)
                    state = page.evaluate("document.readyState", False)
                    if state == "complete":
                        break
                time.sleep(5)
                urls_by_id = {
                    event["params"]["requestId"]: event["params"]["request"]["url"]
                    for event in page.events
                    if event.get("method") == "Network.requestWillBeSent"
                }
                requested = list(urls_by_id.values())
                blocked = [
                    urls_by_id.get(event["params"].get("requestId"), "?")
                    for event in page.events
                    if event.get("method") == "Network.loadingFailed"
                    and "BLOCKED" in str(event["params"].get("errorText", "")).upper()
                ]
                report["blocking"] = {
                    "requested": requested,
                    "blocked": blocked,
                    "blockedCount": len(blocked),
                }
                ad_blocked = any("googlesyndication.com" in url for url in blocked)
                tracker_blocked = any("google-analytics.com" in url for url in blocked)
                control_loaded = any(url.endswith("/pixel.gif") for url in requested)
                if not ad_blocked:
                    raise AssertionError(
                        "Advertising request was not blocked: " + json.dumps(report["blocking"])
                    )
                if not tracker_blocked:
                    raise AssertionError(
                        "Tracker request was not blocked: " + json.dumps(report["blocking"])
                    )
                if not control_loaded:
                    raise AssertionError("The control request was never made; test is invalid")
                report.setdefault("memory", {})["testPageHeapBytes"] = page.heap()
            finally:
                if page is not None:
                    page.close()
                close_page(browser, page_target_id)

            # ---------------------------------------------------------------- #
            # Storage: every setting must be written to extension storage and
            # survive a read-back, and "save all" must flush the tracker engine.
            # The checks run inside the settings page, the same place a user
            # changes these values.
            # ---------------------------------------------------------------- #
            storage_page = open_and_attach(
                browser, port,
                f"chrome-extension://{extension_id}/blueshield-settings.html",
                "document.readyState === 'complete'",
            )
            try:
                storage_result = storage_page.evaluate(
                    r'''(async () => {
                        const askBlocking = message => chrome.runtime.sendMessage(message);
                        const askTracker = message => chrome.runtime.sendMessage({
                            ...message,
                            __blueshieldComponent: 'privacybadger'
                        });
                        const read = async key => (await chrome.storage.local.get(key))[key];
                        const out = {};

                        const before = (await read('rulesetConfig')).showBlockedCount;
                        out.blockingBefore = before;
                        await askBlocking({ what: 'setShowBlockedCount', state: false });
                        await new Promise(r => setTimeout(r, 300));
                        out.blockingAfter = (await read('rulesetConfig')).showBlockedCount;
                        await askBlocking({ what: 'setShowBlockedCount', state: before });
                        await new Promise(r => setTimeout(r, 300));
                        out.blockingRestored = (await read('rulesetConfig')).showBlockedCount;

                        const settingsKey = 'privacybadger.settings_map';
                        await askTracker({ type: 'updateSettings', data: { showCounter: false } });
                        // The tracker engine batches writes; "save all" flushes them.
                        await askTracker({ type: 'syncStorage' });
                        await new Promise(r => setTimeout(r, 400));
                        out.trackerAfter = (await read(settingsKey)).showCounter;
                        await askTracker({ type: 'updateSettings', data: { showCounter: true } });
                        await askTracker({ type: 'syncStorage' });
                        await new Promise(r => setTimeout(r, 400));
                        out.trackerRestored = (await read(settingsKey)).showCounter;

                        out.syncError = (await askTracker({ type: 'syncStorage' })) || null;
                        out.bytesInUse = await chrome.storage.local.getBytesInUse(null);
                        out.keyCount = Object.keys(await chrome.storage.local.get(null)).length;
                        return out;
                    })()'''
                )
            finally:
                storage_page.close()
            report["storage"] = storage_result
            if storage_result["blockingAfter"] is not False:
                raise AssertionError("Blocking setting was not written to storage")
            if storage_result["blockingRestored"] is not True:
                raise AssertionError("Blocking setting could not be restored")
            if storage_result["trackerAfter"] is not False:
                raise AssertionError("Tracker setting was not written to storage")
            if storage_result["trackerRestored"] is not True:
                raise AssertionError("Tracker setting could not be restored")
            if storage_result["syncError"]:
                raise AssertionError(f"Save-all reported an error: {storage_result['syncError']}")
            if storage_result["keyCount"] < 5:
                raise AssertionError("Settings were not persisted to extension storage")

            worker_heap = worker.heap()
            report["memory"] = {"serviceWorkerHeapBytes": worker_heap}

            pages = {}
            page_specs = {
                "popup": (
                    f"chrome-extension://{extension_id}/blueshield-popup.html",
                    "document.readyState === 'complete'",
                ),
                "settings": (
                    f"chrome-extension://{extension_id}/blueshield-settings.html",
                    "document.readyState === 'complete'",
                ),
                "dashboard": (
                    f"chrome-extension://{extension_id}/dashboard.html",
                    "document.readyState === 'complete'",
                ),
                "trackerReport": (
                    f"chrome-extension://{extension_id}/{TRACKER_VENDOR}/skin/popup.html",
                    "window.POPUP_INITIALIZED === true",
                ),
                "trackerSettings": (
                    f"chrome-extension://{extension_id}/{TRACKER_VENDOR}/skin/options.html",
                    "window.OPTIONS_INITIALIZED === true",
                ),
            }
            for name, (url, ready) in page_specs.items():
                client = open_and_attach(browser, port, url, ready)
                try:
                    time.sleep(1.5)
                    info = page_scan(client)
                    info["events"] = [
                        event for event in client.events
                        if event.get("method") in {"Runtime.exceptionThrown", "Log.entryAdded"}
                    ]
                    info["heapBytes"] = client.heap()
                    pages[name] = info
                    assert_clean_ui(name, info)
                    for event in info["events"]:
                        entry = event.get("params", {}).get("entry", {})
                        if entry.get("level") == "error":
                            raise AssertionError(f"{name} logged an error: {entry.get('text')}")
                        if event.get("method") == "Runtime.exceptionThrown":
                            raise AssertionError(f"{name} threw: {event}")
                finally:
                    client.close()

            report["pages"] = pages

            popup = pages["popup"]
            if "BlueShield" not in (popup.get("text") or ""):
                raise AssertionError("Popup did not render the BlueShield name")

            settings = pages["settings"]
            settings_detail = settings_client_state = None
            settings_client = open_and_attach(
                browser, port,
                f"chrome-extension://{extension_id}/blueshield-settings.html",
                "document.readyState === 'complete'",
            )
            try:
                time.sleep(1.5)
                # Walk every section the way a user would, so each one is proven
                # to open and to load its live data.
                expected_tabs = [
                    "overview", "protection", "lists", "custom", "trackers", "storage", "about",
                ]
                sections = {}
                for tab in expected_tabs:
                    sections[tab] = settings_client.evaluate(
                        """(async tab => {
                            document.querySelector('.nav-item[data-tab="' + tab + '"]').click();
                            const panel = document.querySelector('.panel[data-panel="' + tab + '"]');
                            const started = Date.now();
                            while (!panel.classList.contains('active')) {
                                if (Date.now() - started > 5000) {
                                    return { error: 'panel did not open' };
                                }
                                await new Promise(r => setTimeout(r, 100));
                            }
                            await new Promise(r => setTimeout(r, 1500));
                            return {
                                active: panel.classList.contains('active'),
                                text: panel.innerText.slice(0, 400),
                                listRows: panel.querySelectorAll('.list-row').length,
                                chips: panel.querySelectorAll('.chip').length,
                                behaviourToggles: document.querySelectorAll('#behaviour-toggles .toggle').length,
                                trackerToggles: document.querySelectorAll('#tracker-toggles .toggle').length
                            };
                        })("%s")""" % tab,
                    )
                settings_detail = settings_client.evaluate(
                    """({
                        tabs: Array.from(document.querySelectorAll('.nav-item')).map(b => b.dataset.tab),
                        panels: Array.from(document.querySelectorAll('.panel')).map(p => p.dataset.panel),
                        version: document.querySelector('#about-version').textContent,
                        blocking: document.querySelector('#about-blocking').textContent,
                        tracker: document.querySelector('#about-privacy').textContent,
                        overviewLists: document.querySelector('#ov-list-count').textContent,
                        overviewTrackers: document.querySelector('#ov-tracker-count').textContent
                    })""",
                    False,
                )
                settings_detail["sections"] = sections
                settings_client_state = settings_client.heap()
            finally:
                settings_client.close()

            report["settingsPage"] = settings_detail
            if settings_detail["tabs"] != expected_tabs:
                raise AssertionError(f"Settings navigation mismatch: {settings_detail['tabs']}")
            if settings_detail["panels"] != expected_tabs:
                raise AssertionError(f"Settings panels mismatch: {settings_detail['panels']}")
            if settings_detail["version"] != VERSION:
                raise AssertionError(f"Settings show the wrong version: {settings_detail['version']}")
            for tab, detail in sections.items():
                if detail.get("error") or not detail.get("active"):
                    raise AssertionError(f"Settings section failed to open: {tab} {detail}")
            if sections["protection"]["behaviourToggles"] < 5:
                raise AssertionError("Protection section is missing blocking controls")
            if sections["protection"]["trackerToggles"] < 8:
                raise AssertionError("Protection section is missing tracker controls")
            if sections["lists"]["listRows"] < 10:
                raise AssertionError("Filter list section did not load the available lists")
            if settings_detail["overviewLists"] in {"", "–"}:
                raise AssertionError("Settings overview did not load the enabled filter lists")
            if settings_detail["overviewTrackers"] in {"", "–"}:
                raise AssertionError("Settings overview did not load learned trackers")

            dashboard = pages["dashboard"]
            if not dashboard.get("lightClass"):
                raise AssertionError("Dashboard did not switch to the light theme")
            if dashboard.get("primary") != "2 132 199":
                raise AssertionError(f"Dashboard theme is not the BlueShield palette: {dashboard.get('primary')!r}")

            tracker_settings = pages["trackerSettings"]
            if "gradient" not in (tracker_settings.get("bodyBackground") or ""):
                raise AssertionError(
                    f"Tracker settings page is not themed: {tracker_settings.get('bodyBackground')!r}"
                )

            report["memory"]["settingsPageHeapBytes"] = settings_client_state
            report["memory"]["dashboardHeapBytes"] = dashboard["heapBytes"]
            report["memory"]["popupHeapBytes"] = popup["heapBytes"]

            # Memory budget: one shared service worker, no background pages, and
            # pages that are torn down as soon as the user closes them.
            budget = {
                "serviceWorkerHeapBytes": 96 * 1024 * 1024,
                "settingsPageHeapBytes": 64 * 1024 * 1024,
                "dashboardHeapBytes": 64 * 1024 * 1024,
                "popupHeapBytes": 32 * 1024 * 1024,
            }
            for key, limit in budget.items():
                used = report["memory"].get(key)
                if used is None:
                    continue
                if used > limit:
                    raise AssertionError(
                        f"{key} is {used / 1048576:.1f} MB, over the {limit / 1048576:.0f} MB budget"
                    )
            extra_background = [
                target for target in target_list(port)
                if target.get("type") in {"background_page", "shared_worker", "service_worker"}
                and "blueshield-service-worker.js" not in target.get("url", "")
            ]
            if extra_background:
                raise AssertionError(
                    "Unexpected extra background contexts: "
                    + json.dumps([t.get("url") for t in extra_background])
                )

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
                process.wait(timeout=5)
            REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
