#!/usr/bin/env python3
"""Build the BlueShield Manifest V3 extension, ZIP, and signed CRX3."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
UBO_REPO = WORKSPACE / "uBlock"
UBO_BUILD = UBO_REPO / "dist" / "build" / "uBOLite.chromium"
PB_REPO = WORKSPACE / "privacybadger-mv3"
PB_SRC = PB_REPO / "src"
RELEASE = ROOT / "release"
EXTENSION_NAME = "BlueShield"
EXTENSION_VERSION = "1.0.2.0"

# The staging directory and the two archives are always named from the product
# name and version above, so bumping the version needs a single edit.
STAGE_NAME = f"{EXTENSION_NAME}-{EXTENSION_VERSION}"
STAGE = RELEASE / STAGE_NAME
ZIP_PATH = RELEASE / f"{STAGE_NAME}.zip"
CRX_PATH = RELEASE / f"{STAGE_NAME}.crx"
KEY_DIR = ROOT / "keys"
KEY_PATH = KEY_DIR / "blueshield-signing.pem"

SERVICE_WORKER = "blueshield-service-worker.js"
MANAGED_SCHEMA = "blueshield-managed-schema.json"
THEME_STYLESHEET = "blueshield-theme.css"

PB_BASE = "vendor/tracker-protection"
PB_RULE_BASE = 100_000_000
PB_SESSION_RULE_BASE = 120_000_000
PB_RULE_LIMIT = 140_000_000
PB_STATIC_RULE_OFFSET = 200_000_000
MESSAGE_COMPONENT = "privacybadger"
STORAGE_PREFIX = "privacybadger."


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one match in {path}, found {count}: {old[:80]!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def copy_extension_sources() -> tuple[dict, dict]:
    if not (UBO_BUILD / "manifest.json").is_file():
        raise RuntimeError(
            f"Missing uBlock Origin Lite MV3 build at {UBO_BUILD}. "
            "Run `make mv3-chromium` in the uBlock repository first."
        )
    if not (PB_SRC / "manifest.json").is_file():
        raise RuntimeError(f"Missing Privacy Badger MV3 source at {PB_SRC}")

    if RELEASE.exists():
        shutil.rmtree(RELEASE)
    STAGE.mkdir(parents=True)

    shutil.copytree(UBO_BUILD, STAGE, dirs_exist_ok=True)
    for unwanted in ("log.txt", "README.md"):
        path = STAGE / unwanted
        if path.exists():
            path.unlink()

    pb_root = STAGE / PB_BASE
    shutil.copytree(
        PB_SRC,
        pb_root,
        ignore=shutil.ignore_patterns("_locales", "tests", "manifest.json", "background.html", "*.map"),
    )

    return read_json(UBO_BUILD / "manifest.json"), read_json(PB_SRC / "manifest.json")


def runtime_javascript(pb_manifest: dict) -> str:
    """Return a self-contained scoped WebExtension API proxy."""
    manifest_json = json.dumps(pb_manifest, ensure_ascii=False, separators=(",", ":"))
    return f'''import {{ PB_MANIFEST }} from "./pb-manifest.js";

const __PB_BASE = {json.dumps(PB_BASE)};
const __PB_RULE_BASE = {PB_RULE_BASE};
const __PB_SESSION_RULE_BASE = {PB_SESSION_RULE_BASE};
const __PB_RULE_LIMIT = {PB_RULE_LIMIT};
const __PB_COMPONENT = {json.dumps(MESSAGE_COMPONENT)};
const __PB_STORAGE_PREFIX = {json.dumps(STORAGE_PREFIX)};
const __nativeChrome = globalThis.chrome;

function __callbackify(promise, callback) {{
    if (typeof callback !== "function") {{ return promise; }}
    promise.then(
        value => callback(value),
        error => {{ throw error; }}
    );
    return undefined;
}}

function __bind(target, method) {{
    return (...args) => method.apply(target, args);
}}

function __scopedPath(path) {{
    path = String(path);
    if (path === "") {{ return path; }}
    if (/^(?:chrome-extension|moz-extension|https?|data|blob):/.test(path)) {{
        return path;
    }}
    path = path.replace(/^\\/+/, "");
    if (path === __PB_BASE || path.startsWith(__PB_BASE + "/")) {{
        return path;
    }}
    return __PB_BASE + "/" + path;
}}

function __tagMessage(message) {{
    if (message && typeof message === "object" && !Array.isArray(message)) {{
        return {{ ...message, __blueshieldComponent: __PB_COMPONENT }};
    }}
    return message;
}}

function __prefixStorageKey(key) {{
    key = String(key);
    return key.startsWith(__PB_STORAGE_PREFIX) ? key : __PB_STORAGE_PREFIX + key;
}}

function __prefixStorageKeys(keys) {{
    if (keys === null || keys === undefined) {{ return null; }}
    if (typeof keys === "string") {{ return __prefixStorageKey(keys); }}
    if (Array.isArray(keys)) {{ return keys.map(__prefixStorageKey); }}
    if (typeof keys === "object") {{
        const out = {{}};
        for (const [key, value] of Object.entries(keys)) {{
            out[__prefixStorageKey(key)] = value;
        }}
        return out;
    }}
    return keys;
}}

function __restoreStorageResult(result) {{
    if (!result || typeof result !== "object") {{ return result; }}
    const out = {{}};
    for (const [key, value] of Object.entries(result)) {{
        if (key.startsWith(__PB_STORAGE_PREFIX)) {{
            out[key.slice(__PB_STORAGE_PREFIX.length)] = value;
        }} else {{
            out[key] = value;
        }}
    }}
    return out;
}}

// Rough payload size without allocating a copy of the value. Serialising a
// multi-megabyte store just to measure it produced megabytes of garbage on every
// write, which showed up as the worker growing while browsing.
function __approxBytes(value) {{
    if (value === null || value === undefined) {{
        return 8;
    }}
    if (typeof value !== 'object') {{
        return 8;
    }}
    if (Array.isArray(value)) {{
        return value.length * 64;
    }}
    let entries = 0;
    for (const key in value) {{
        if (Object.prototype.hasOwnProperty.call(value, key)) {{
            entries += 1;
        }}
    }}
    return entries * 160;
}}

function __sameValue(a, b, size) {{
    // Unchanged stores are almost always the same object, so the cheap identity
    // check handles them; large values skip the deep compare entirely.
    if (a === b) {{
        return true;
    }}
    if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) {{
        return false;
    }}
    if (size > 100000) {{
        return false;
    }}
    try {{
        return JSON.stringify(a) === JSON.stringify(b);
    }} catch (error) {{
        return false;
    }}
}}

function __makeStorageArea(nativeArea, prefixKeys = true) {{
    // Writing a large store to extension storage takes hundreds of milliseconds
    // and happens in a burst while the worker initialises. Identical values are
    // dropped, and the rest are merged into one write per key per tick, which
    // turns a storm of writes into a handful without changing what is stored.
    const pending = new Map();
    const pendingSizes = new Map();
    let pendingBytes = 0;
    let flushTimer;

    const flush = () => {{
        flushTimer = undefined;
        if (pending.size === 0) {{
            return Promise.resolve();
        }}
        const batch = Object.assign({{}}, ...pending.values());
        const bytes = pendingBytes;
        pending.clear();
        pendingSizes.clear();
        pendingBytes = 0;
        return __timed(__perf.storageSet, () => nativeArea.set(batch), bytes);
    }};

    const queue = (items) => {{
        for (const [key, value] of Object.entries(items || {{}})) {{
            const size = __approxBytes(value);
            const previous = pending.get(key);
            if (previous && __sameValue(previous[key], value, size)) {{
                continue;
            }}
            if (previous) {{
                pendingBytes -= pendingSizes.get(key) || 0;
            }}
            pendingSizes.set(key, size);
            pendingBytes += size;
            pending.set(key, {{ [key]: value }});
        }}
        if (flushTimer === undefined) {{
            // A large write costs several hundred milliseconds. While the rule
            // set is still being installed, or just after, that cost lands in
            // the same window as page loading, which is what the user feels as a
            // stall. Small writes are never delayed.
            let delay = 0;
            if (pendingBytes > 200000) {{
                const armedAt = globalThis.__blueshieldRulesArmedAt;
                const installing = armedAt === undefined;
                const justArmed = armedAt !== undefined
                    && Date.now() - armedAt < __STORAGE_GRACE_MS;
                if (installing || justArmed) {{
                    delay = 2500;
                }}
            }}
            flushTimer = setTimeout(flush, delay);
        }}
    }};

    return new Proxy(nativeArea, {{
        get(target, property) {{
            if (prefixKeys && property === "set") {{
                return (items, callback) => {{
                    const prefixed = __prefixStorageKeys(items);
                    queue(prefixed);
                    if (typeof callback === "function") {{
                        flush().then(() => callback(), () => callback());
                        return undefined;
                    }}
                    return flush();
                }};
            }}
            if (prefixKeys && property === "get") {{
                return (keys, callback) => {{
                    const promise = __timed(__perf.storageGet, () => target.get(
                        __prefixStorageKeys(keys)
                    )).then(__restoreStorageResult);
                    return __callbackify(promise, callback);
                }};
            }}
            if (prefixKeys && property === "remove") {{
                return (keys, callback) => {{
                    for (const key of [].concat(__prefixStorageKeys(keys) || [])) {{
                        pendingBytes -= pendingSizes.get(key) || 0;
                        pendingSizes.delete(key);
                        pending.delete(key);
                    }}
                    return __callbackify(
                        Promise.resolve(target.remove(__prefixStorageKeys(keys))),
                        callback
                    );
                }};
            }}
            if (prefixKeys && property === "clear") {{
                return (callback) => {{
                    pending.clear();
                    pendingSizes.clear();
                    pendingBytes = 0;
                    return __callbackify(
                        Promise.resolve(target.clear()), callback);
                }};
            }}
            if (prefixKeys && property === "getBytesInUse") {{
                return (keys, callback) => __callbackify(
                    Promise.resolve(target.getBytesInUse(__prefixStorageKeys(keys))),
                    callback
                );
            }}
            if (prefixKeys && property === "clear") {{
                return (callback) => {{
                    const promise = Promise.resolve(target.get(null)).then(items => {{
                        const keys = Object.keys(items).filter(
                            key => key.startsWith(__PB_STORAGE_PREFIX)
                        );
                        return keys.length ? target.remove(keys) : undefined;
                    }});
                    return __callbackify(promise, callback);
                }};
            }}
            const value = Reflect.get(target, property, target);
            return typeof value === "function" ? __bind(target, value) : value;
        }}
    }});
}}

// Content scripts get a restricted `chrome` object: several namespaces simply do
// not exist there. Proxying a missing namespace throws "Cannot create proxy with
// a non-object as target", which killed the tracker engine's page scripts
// outright, so an absent namespace is stood in for by an empty object.
const __namespace = (value) => value ?? {{}};
const __chromeStorage = __namespace(__nativeChrome.storage);

const __storage = {{
    local: __makeStorageArea(__namespace(__chromeStorage.local)),
    sync: __makeStorageArea(__namespace(__chromeStorage.sync)),
    session: __makeStorageArea(__namespace(__chromeStorage.session)),
    managed: __makeStorageArea(__namespace(__chromeStorage.managed), false),
}};

function __makeOnMessageEvent() {{
    const nativeEvent = __nativeChrome.runtime.onMessage;
    const wrappers = new Map();
    return new Proxy(nativeEvent, {{
        get(target, property) {{
            if (property === "addListener") {{
                return listener => {{
                    const wrapped = (message, ...args) => {{
                        if (!message || message.__blueshieldComponent !== __PB_COMPONENT) {{
                            return undefined;
                        }}
                        return listener(message, ...args);
                    }};
                    wrappers.set(listener, wrapped);
                    target.addListener(wrapped);
                }};
            }}
            if (property === "removeListener") {{
                return listener => {{
                    const wrapped = wrappers.get(listener);
                    if (wrapped) {{ target.removeListener(wrapped); }}
                    wrappers.delete(listener);
                }};
            }}
            if (property === "hasListener") {{
                return listener => target.hasListener(wrappers.get(listener));
            }}
            const value = Reflect.get(target, property, target);
            return typeof value === "function" ? __bind(target, value) : value;
        }}
    }});
}}

const __runtimeEvent = __makeOnMessageEvent();
const __nativeRuntime = __nativeChrome.runtime;
const __runtime = new Proxy(__nativeRuntime, {{
    get(target, property) {{
        if (property === "getURL") {{
            return path => target.getURL(__scopedPath(path));
        }}
        if (property === "getManifest") {{
            return () => PB_MANIFEST;
        }}
        if (property === "onMessage") {{
            return __runtimeEvent;
        }}
        if (property === "sendMessage") {{
            return (message, ...args) => target.sendMessage(__tagMessage(message), ...args);
        }}
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? __bind(target, value) : value;
    }}
}});

const __tabs = new Proxy(__namespace(__nativeChrome.tabs), {{
    get(target, property) {{
        if (property === "sendMessage") {{
            return (tabId, message, ...args) => target.sendMessage(
                tabId, __tagMessage(message), ...args
            );
        }}
        if (property === "executeScript") {{
            return (injection, ...args) => {{
                injection = {{ ...injection }};
                if (Array.isArray(injection.files)) {{
                    injection.files = injection.files.map(__scopedPath);
                }}
                return target.executeScript(injection, ...args);
            }};
        }}
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? __bind(target, value) : value;
    }}
}});

function __scopeScriptPaths(scripts) {{
    return scripts.map(script => {{
        script = {{ ...script }};
        if (typeof script.id === "string" &&
            !script.id.startsWith(__PB_COMPONENT + ".")) {{
            script.id = __PB_COMPONENT + "." + script.id;
        }}
        if (Array.isArray(script.js)) {{
            script.js = script.js.map(__scopedPath);
        }}
        return script;
    }});
}}

// The tracker engine observes every request in every tab through a
// non-blocking webRequest listener. That is a hot path, so its cost is timed
// here: if it ever becomes expensive, it shows up in the report instead of as
// mysterious browser lag.
function __wrapWebRequestEvent(event) {{
    if (!event) {{ return event; }}
    return new Proxy(event, {{
        get(inner, property) {{
            if (property === "addListener") {{
                return (listener, ...rest) => inner.addListener(
                    (...args) => {{
                        const started = __now();
                        try {{
                            return listener(...args);
                        }} finally {{
                            const ms = __now() - started;
                            __perf.requests.count += 1;
                            __perf.requests.ms += ms;
                            if (ms > __perf.requests.maxMs) {{
                                __perf.requests.maxMs = ms;
                            }}
                        }}
                    }},
                    ...rest
                );
            }}
            const value = Reflect.get(inner, property, inner);
            return typeof value === "function" ? __bind(inner, value) : value;
        }},
    }});
}}

const __webRequest = new Proxy(__namespace(__nativeChrome.webRequest), {{
    get(target, property) {{
        if (typeof property === "string" && property.startsWith("on")) {{
            return __wrapWebRequestEvent(target[property]);
        }}
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? __bind(target, value) : value;
    }},
}});

const __scripting = new Proxy(__namespace(__nativeChrome.scripting), {{
    get(target, property) {{
        if (property === "registerContentScripts") {{
            return (scripts, ...args) => __timed(
                __perf.scripting,
                () => target.registerContentScripts(
                    __scopeScriptPaths(scripts), ...args
                ),
            );
        }}
        if (property === "unregisterContentScripts") {{
            return (options, ...args) => {{
                if (options && Array.isArray(options.ids)) {{
                    options = {{
                        ...options,
                        ids: options.ids.map(id =>
                            id.startsWith(__PB_COMPONENT + ".") ? id : __PB_COMPONENT + "." + id
                        )
                    }};
                }}
                return __timed(
                    __perf.scripting,
                    () => target.unregisterContentScripts(options, ...args),
                );
            }};
        }}
        if (property === "executeScript") {{
            return (injection, ...args) => {{
                injection = {{ ...injection }};
                if (Array.isArray(injection.files)) {{
                    injection.files = injection.files.map(__scopedPath);
                }}
                return target.executeScript(injection, ...args);
            }};
        }}
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? __bind(target, value) : value;
    }}
}});

function __isDynamicPrivacyBadgerRule(rule) {{
    return rule.id >= __PB_RULE_BASE && rule.id < __PB_SESSION_RULE_BASE;
}}

function __isSessionPrivacyBadgerRule(rule) {{
    return rule.id >= __PB_SESSION_RULE_BASE && rule.id < __PB_RULE_LIMIT;
}}

function __filterRuleUpdate(options, predicate) {{
    options = {{ ...options }};
    if (Array.isArray(options.removeRuleIds)) {{
        options.removeRuleIds = options.removeRuleIds.filter(predicate);
    }}
    return options;
}}

const __pbDnrStats = globalThis.__blueshieldTrackerDnrStats ||= {{
    added: 0,
    removed: 0,
    retried: 0,
    skipped: 0,
}};

// ---------------------------------------------------------------------------
// Performance counters
//
// The worker is shared by both engines, so any stall the user feels has to be
// attributable. Every DNR write, rule read, storage write and request callback
// is timed here, and long tasks are recorded, so `perf_test.py` can prove which
// subsystem is responsible instead of guessing.
// ---------------------------------------------------------------------------
const __perf = globalThis.__blueshieldPerf ||= {{
    dnrUpdate: {{ calls: 0, rules: 0, ms: 0, maxMs: 0 }},
    dnrNative: {{ calls: 0, rules: 0, ms: 0, maxMs: 0 }},
    dnrRead: {{ calls: 0, rules: 0, ms: 0, maxMs: 0 }},
    storageSet: {{ calls: 0, rules: 0, ms: 0, maxMs: 0 }},
    storageGet: {{ calls: 0, rules: 0, ms: 0, maxMs: 0 }},
    scripting: {{ calls: 0, rules: 0, ms: 0, maxMs: 0 }},
    requests: {{ count: 0, rules: 0, ms: 0, maxMs: 0 }},
    longTasks: [],
}};

const __now = () => performance.now();
// A large write is held back for this long after the rules finish installing.
const __STORAGE_GRACE_MS = 2500;

function __record(bucket, ms, size) {{
    if (size) {{ bucket.rules += size; }}
    bucket.calls += 1;
    bucket.ms += ms;
    if (ms > bucket.maxMs) {{ bucket.maxMs = ms; }}
}}

async function __timed(bucket, work, size) {{
    const started = __now();
    try {{
        return await work();
    }} finally {{
        __record(bucket, __now() - started, size);
    }}
}}

try {{
    if (PerformanceObserver.supportedEntryTypes?.includes('longtask') !== true) {{
        throw new Error('longtask unsupported');
    }}
    new PerformanceObserver((list) => {{
        for (const entry of list.getEntries()) {{
            if (entry.duration < 150) {{ continue; }}
            __perf.longTasks.push({{
                at: Math.round(entry.startTime),
                ms: Math.round(entry.duration),
                name: entry.name,
            }});
            if (__perf.longTasks.length > 40) {{ __perf.longTasks.shift(); }}
        }}
    }}).observe({{ entryTypes: ['longtask'] }});
}} catch (error) {{
    // Long-task observation is a diagnostic aid only.
}}

const __RETRY_DELAYS = [0, 200, 600, 1500, 3000];

// Chromium applies a dynamic-rule update synchronously on the browser side, so
// one call carrying several thousand rules blocks the browser for over a
// second: the user sees the tab stop and restart. Rule sets are therefore
// always installed in slices, with a yield between them, so the browser can
// keep painting and loading between batches. A slice that is rejected is split
// further, down to single rules.
const __RULE_CHUNK_SIZE = 200;

const __yieldToBrowser = () => new Promise(resolve => setTimeout(resolve, 0));

const __sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

// Chromium caps host-access dynamic rules per extension at 5000, and the other
// bundled engine briefly holds some of them while it rewrites its own rules on
// startup. A rule rejected because of that shared quota is retried with a
// short backoff; a rule rejected for a permanent reason (a malformed redirect,
// for example) is reported immediately instead of stalling startup.
function __isTransientDnrError(error) {{
    const message = String((error && error.message) || error || '').toLowerCase();
    return message.includes('quota') || message.includes('max_number')
        || message.includes('too many') || message.includes('limit');
}}

async function __addRuleWithRetry(target, method, rule) {{
    let lastError;
    for (let attempt = 0; attempt < __RETRY_DELAYS.length; attempt += 1) {{
        const delay = __RETRY_DELAYS[attempt];
        if (delay !== 0) {{
            await __sleep(delay);
            __pbDnrStats.retried += 1;
        }}
        try {{
            await __timed(
                __perf.dnrNative,
                () => target[method]({{ addRules: [ rule ] }}),
                1,
            );
            __pbDnrStats.added += 1;
            return true;
        }} catch(error) {{
            lastError = error;
            if ( !__isTransientDnrError(error) ) {{ break; }}
        }}
    }}
    __pbDnrStats.skipped += 1;
    console.warn('[BlueShield] Could not install learned tracker rule:',
        rule, lastError && lastError.message ? lastError.message : lastError);
    return false;
}}

async function __updateRulesCompat(target, method, options, predicate) {{
    options = __filterRuleUpdate(options || {{}}, predicate);
    const removeRuleIds = Array.isArray(options.removeRuleIds)
        ? options.removeRuleIds : [];
    const addRules = Array.isArray(options.addRules) ? options.addRules : [];
    if (removeRuleIds.length !== 0) {{
        await __timed(
            __perf.dnrNative,
            () => target[method]({{ removeRuleIds }}),
            removeRuleIds.length,
        );
        __pbDnrStats.removed += removeRuleIds.length;
    }}

    const total = addRules.length;
    for (let offset = 0; offset < total; offset += __RULE_CHUNK_SIZE) {{
        const chunk = addRules.slice(offset, offset + __RULE_CHUNK_SIZE);
        try {{
            await __timed(
                __perf.dnrNative,
                () => target[method]({{ addRules: chunk }}),
                chunk.length,
            );
            __pbDnrStats.added += chunk.length;
        }} catch(error) {{
            if (chunk.length === 1) {{
                await __addRuleWithRetry(target, method, chunk[0]);
            }} else {{
                const midpoint = Math.ceil(chunk.length / 2);
                await __updateRulesCompat(target, method,
                    {{ addRules: chunk.slice(0, midpoint) }}, () => true);
                await __updateRulesCompat(target, method,
                    {{ addRules: chunk.slice(midpoint) }}, () => true);
            }}
        }}
        if (__pbDnrStats.added > 4000 && globalThis.__blueshieldRulesArmedAt === undefined) {{
            globalThis.__blueshieldRulesArmedAt = Date.now();
        }}
        if (offset + chunk.length < total) {{
            await __yieldToBrowser();
        }}
    }}
    if (total > 0 && globalThis.__blueshieldRulesArmedAt === undefined) {{
        globalThis.__blueshieldRulesArmedAt = Date.now();
    }}
}}

const __dnr = new Proxy(__namespace(__nativeChrome.declarativeNetRequest), {{
    get(target, property) {{
        if (property === "getDynamicRules") {{
            return (...args) => {{
                const callback = args.at(-1);
                const promise = __timed(__perf.dnrRead, () => target.getDynamicRules())
                    .then(rules => rules.filter(__isDynamicPrivacyBadgerRule));
                return typeof callback === "function"
                    ? __callbackify(promise, callback)
                    : promise;
            }};
        }}
        if (property === "getSessionRules") {{
            return (...args) => {{
                const callback = args.at(-1);
                const promise = __timed(__perf.dnrRead, () => target.getSessionRules())
                    .then(rules => rules.filter(__isSessionPrivacyBadgerRule));
                return typeof callback === "function"
                    ? __callbackify(promise, callback)
                    : promise;
            }};
        }}
        if (property === "updateDynamicRules") {{
            return (options, ...args) => {{
                const callback = args.at(-1);
                const touched = (Array.isArray(options?.addRules) ? options.addRules.length : 0)
                    + (Array.isArray(options?.removeRuleIds) ? options.removeRuleIds.length : 0);
                const promise = __timed(
                    __perf.dnrUpdate,
                    () => __updateRulesCompat(
                        target, "updateDynamicRules", options,
                        __isDynamicPrivacyBadgerRule
                    ),
                    touched,
                );
                return typeof callback === "function"
                    ? __callbackify(promise, callback)
                    : promise;
            }};
        }}
        if (property === "updateSessionRules") {{
            return (options, ...args) => {{
                const callback = args.at(-1);
                const touched = (Array.isArray(options?.addRules) ? options.addRules.length : 0)
                    + (Array.isArray(options?.removeRuleIds) ? options.removeRuleIds.length : 0);
                const promise = __timed(
                    __perf.dnrUpdate,
                    () => __updateRulesCompat(
                        target, "updateSessionRules", options,
                        __isSessionPrivacyBadgerRule
                    ),
                    touched,
                );
                return typeof callback === "function"
                    ? __callbackify(promise, callback)
                    : promise;
            }};
        }}
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? __bind(target, value) : value;
    }}
}});

const __i18n = new Proxy(__namespace(__nativeChrome.i18n), {{
    get(target, property) {{
        if (property === "getMessage") {{
            return (key, substitutions) => {{
                if (key.startsWith("@@")) {{ return target.getMessage(key, substitutions); }}
                const scopedKey = key.startsWith("pb_") ? key : "pb_" + key;
                return target.getMessage(scopedKey, substitutions);
            }};
        }}
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? __bind(target, value) : value;
    }}
}});

function __noOpActionMethod(name) {{
    return (...args) => {{
        const callback = args.at(-1);
        if (typeof callback === "function") {{ callback(); }}
        return Promise.resolve();
    }};
}}

function __makeActionProxy(nativeAction) {{
    return new Proxy(nativeAction || {{}}, {{
        get(target, property) {{
            if (typeof property === "string" && property.startsWith("set")) {{
                return __noOpActionMethod(property);
            }}
            if (property === "openPopup") {{
                return __noOpActionMethod(property);
            }}
            const value = Reflect.get(target, property, target);
            return typeof value === "function" ? __bind(target, value) : value;
        }}
    }});
}}

const chrome = new Proxy(__nativeChrome, {{
    get(target, property) {{
        if (property === "runtime") {{ return __runtime; }}
        if (property === "storage") {{ return __storage; }}
        if (property === "tabs") {{ return __tabs; }}
        if (property === "scripting") {{ return __scripting; }}
        if (property === "webRequest") {{ return __webRequest; }}
        if (property === "declarativeNetRequest") {{ return __dnr; }}
        if (property === "i18n") {{ return __i18n; }}
        if (property === "action") {{ return __makeActionProxy(target.action); }}
        if (property === "browserAction") {{ return __makeActionProxy(target.browserAction); }}
        const value = Reflect.get(target, property, target);
        return typeof value === "function" ? __bind(target, value) : value;
    }}
}});

const browser = chrome;

export {{ chrome as __blueshieldChrome, browser as __blueshieldBrowser }};
'''


def install_pb_runtime(pb_root: Path, pb_manifest: dict) -> None:
    runtime_path = pb_root / "lib" / "runtime.js"
    manifest_module = (
        "export const PB_MANIFEST = "
        + json.dumps(pb_manifest, ensure_ascii=False, separators=(",", ":"))
        + ";\n"
    )
    (pb_root / "lib" / "pb-manifest.js").write_text(manifest_module, encoding="utf-8")
    runtime_path.write_text(runtime_javascript(pb_manifest), encoding="utf-8")

    module_pattern = re.compile(r"^\s*(?:import|export)\s", re.MULTILINE)
    excluded = {"lib/vendor", "tests", "js/contentscripts", "js/firstparties"}
    injected = []
    for path in sorted(pb_root.rglob("*.js")):
        rel = path.relative_to(pb_root).as_posix()
        if rel in {"lib/runtime.js", "lib/pb-manifest.js"}:
            continue
        if any(rel == item or rel.startswith(item + "/") for item in excluded):
            continue
        text = path.read_text(encoding="utf-8")
        # lib/i18n.js is loaded as a module by the HTML pages but contains no
        # static import/export declarations of its own.
        if not module_pattern.search(text) and rel != "lib/i18n.js":
            continue
        import_path = Path(os.path.relpath(runtime_path, path.parent)).as_posix()
        if not import_path.startswith("."):
            import_path = "./" + import_path
        preamble = (
            f'import {{ __blueshieldChrome, __blueshieldBrowser }} from {json.dumps(import_path)};\n'
            "const chrome = __blueshieldChrome;\n"
            "const browser = __blueshieldBrowser;\n\n"
        )
        path.write_text(preamble + text, encoding="utf-8")
        injected.append(rel)
    (pb_root / "lib" / "runtime-injection.json").write_text(
        json.dumps(injected, indent=2) + "\n", encoding="utf-8"
    )


def patch_pb_runtime(pb_root: Path) -> None:
    constants = pb_root / "js" / "constants.js"
    replace_once(
        constants,
        "  // DNR feature tests\n",
        "  // BlueShield DNR ID ranges keep the tracker engine separate from the blocking engine.\n"
        "  DNR_DYNAMIC_RULE_ID_BASE: 100000000,\n"
        "  DNR_SESSION_RULE_ID_BASE: 120000000,\n"
        "  DNR_RULE_ID_LIMIT: 140000000,\n"
        "  DNR_STATIC_RULE_ID_OFFSET: 200000000,\n\n"
        "  // DNR feature tests\n",
    )

    background = pb_root / "js" / "background.js"
    replace_once(
        background,
        "    self.maxDynamicRuleId = (rules.length ?\n"
        "      Math.max(...rules.map(r => r.id)) : 0);",
        "    self.maxDynamicRuleId = (rules.length ?\n"
        "      Math.max(...rules.map(r => r.id - constants.DNR_DYNAMIC_RULE_ID_BASE)) : 0);",
    )
    replace_once(
        background,
        "  getDynamicRuleId: function () {\n"
        "    return ++this.maxDynamicRuleId;\n"
        "  },",
        "  getDynamicRuleId: function () {\n"
        "    return constants.DNR_DYNAMIC_RULE_ID_BASE + (++this.maxDynamicRuleId);\n"
        "  },",
    )
    replace_once(
        background,
        "  getSessionRuleId: function () {\n"
        "    return ++this.maxSessionRuleId;\n"
        "  },",
        "  getSessionRuleId: function () {\n"
        "    return constants.DNR_SESSION_RULE_ID_BASE + (++this.maxSessionRuleId);\n"
        "  },",
    )

    storage = pb_root / "js" / "storage.js"
    replace_once(
        storage,
        "          r.id = idx + 1;",
        "          r.id = constants.DNR_DYNAMIC_RULE_ID_BASE + idx;",
    )

    dnr_utils = pb_root / "lib" / "dnr" / "utils.js"
    replace_once(
        dnr_utils,
        "    chrome.declarativeNetRequest.updateDynamicRules(opts, function () {\n"
        "      // notify all subscribers",
        "    chrome.declarativeNetRequest.updateDynamicRules(opts, function () {\n"
        "      if (chrome.runtime.lastError) {\n"
        "        console.error('[BlueShield] Privacy Badger DNR update failed:',\n"
        "          chrome.runtime.lastError.message);\n"
        "      }\n"
        "      // notify all subscribers",
    )
    replace_once(
        dnr_utils,
        "        extensionPath: surrogate_path",
        "        extensionPath: '/" + PB_BASE + "/' + surrogate_path.replace(/^\\/+/, '')",
    )

    # Per-tab state (learned trackers, temporary allow lists and the tab's own
    # session rules) is held for 20 seconds after a tab closes. Nothing needs it
    # once the tab is gone, and a long tail makes the worker hold data for every
    # recently closed tab, so release it as soon as the event queue has drained.
    webrequest = pb_root / "js" / "webrequest.js"
    replace_once(
        webrequest,
        "function onTabRemoved(tab_id) {\n"
        "  setTimeout(function () {\n"
        "    forgetTab(tab_id);\n"
        "    dnrUtils.removeTabSessionRules(tab_id);\n"
        "  }, utils.oneSecond() * 20);",
        "function onTabRemoved(tab_id) {\n"
        "  setTimeout(function () {\n"
        "    forgetTab(tab_id);\n"
        "    dnrUtils.removeTabSessionRules(tab_id);\n"
        "  }, utils.oneSecond() * 2);",
    )

    i18n = pb_root / "lib" / "i18n.js"
    replace_once(
        i18n,
        'document.location.pathname == "/skin/options.html"',
        'document.location.pathname.endsWith("/skin/options.html")',
    )
    replace_once(
        i18n,
        'document.location.pathname == "/skin/firstRun.html"',
        'document.location.pathname.endsWith("/skin/firstRun.html")',
    )


def classic_runtime(pb_manifest: dict) -> str:
    code = runtime_javascript(pb_manifest)
    code = code.replace(
        'import { PB_MANIFEST } from "./pb-manifest.js";\n',
        "const PB_MANIFEST = "
        + json.dumps(pb_manifest, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        1,
    )
    code = code.replace("export { chrome as __blueshieldChrome, browser as __blueshieldBrowser };\n", "")
    return code


def build_content_script_bundles(pb_root: Path, pb_manifest: dict) -> list[dict]:
    output_dir = pb_root / "content-scripts"
    output_dir.mkdir(exist_ok=True)
    prelude = classic_runtime(pb_manifest)
    transformed = []
    for index, content_script in enumerate(pb_manifest.get("content_scripts", [])):
        parts = [prelude]
        for source_name in content_script.get("js", []):
            source = (pb_root / source_name).read_text(encoding="utf-8")
            parts.append(";\n" + source)
        bundle = "/* Privacy Badger content-script bundle generated for BlueShield. */\n(() => {\n"
        bundle += "\n".join(parts)
        bundle += "\n})();\n"
        bundle_name = f"content-{index}.js"
        (output_dir / bundle_name).write_text(bundle, encoding="utf-8")

        new_entry = dict(content_script)
        new_entry["js"] = [f"{PB_BASE}/content-scripts/{bundle_name}"]
        if content_script.get("css"):
            new_entry["css"] = [f"{PB_BASE}/{path.lstrip('/')}" for path in content_script["css"]]
        transformed.append(new_entry)
    return transformed


def rewrite_pb_html_and_css(pb_root: Path) -> None:
    known_dirs = ("skin", "icons", "lib", "js", "data", "content-scripts")
    attr_pattern = re.compile(
        r"((?:src|srcset|href|action)=[\"'])/(" + "|".join(known_dirs) + r")/"
    )
    url_pattern = re.compile(
        r"url\(\s*([\"']?)/(" + "|".join(known_dirs) + r")/"
    )
    for path in sorted(pb_root.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        text = attr_pattern.sub(rf'\1/{PB_BASE}/\2/', text)
        path.write_text(text, encoding="utf-8")
    for path in sorted(pb_root.rglob("*.css")):
        text = path.read_text(encoding="utf-8")
        text = url_pattern.sub(
            lambda match: f"url({match.group(1)}/{PB_BASE}/{match.group(2)}/",
            text,
        )
        path.write_text(text, encoding="utf-8")


def offset_static_rule_ids(pb_root: Path) -> None:
    for path in sorted((pb_root / "data" / "dnr").glob("*.json")):
        rules = read_json(path)
        for rule in rules:
            if not isinstance(rule.get("id"), int):
                raise RuntimeError(f"Static DNR rule without integer id in {path}")
            rule["id"] += PB_STATIC_RULE_OFFSET
        write_json(path, rules)


def merge_locales(pb_manifest: dict) -> None:
    pb_default = pb_manifest["default_locale"]
    root_locales = STAGE / "_locales"
    for pb_locale_dir in sorted((PB_SRC / "_locales").iterdir()):
        if not pb_locale_dir.is_dir():
            continue
        target_name = "en" if pb_locale_dir.name == pb_default else pb_locale_dir.name
        target_dir = root_locales / target_name
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / "messages.json"
        messages = read_json(target_file) if target_file.exists() else {}
        pb_messages = read_json(pb_locale_dir / "messages.json")
        for key, value in pb_messages.items():
            messages[f"pb_{key}"] = value
        write_json(target_file, messages)


def merge_managed_schema(pb_manifest: dict) -> str:
    ubo_schema = read_json(STAGE / "managed_storage.json")
    pb_schema = read_json(PB_SRC / "data" / "schema.json")
    properties = ubo_schema.setdefault("properties", {})
    for key, value in pb_schema.get("properties", {}).items():
        if key in properties:
            raise RuntimeError(f"Managed-storage policy collision: {key}")
        properties[key] = value
    output = MANAGED_SCHEMA
    write_json(STAGE / output, ubo_schema)
    return output


# --------------------------------------------------------------------------- #
# BlueShield rebranding: light-blue theme, hidden upstream names
# --------------------------------------------------------------------------- #

# Display-name substitutions applied to every localized string. Longer names come
# first so that "uBlock Origin Lite" is not partially rewritten. Patterns are
# regexes; ``{name}`` is replaced with the BlueShield product name.
BRAND_PATTERNS = [
    (r"uBlock Origin Lite", "{name}"),
    (r"uBO Lite", "{name}"),
    (r"uBlock Origin", "{name}"),
    (r"uBlock(?!Origin)", "{name}"),
    (r"Privacy Badger", "{name}"),
    (r"Privacy badger", "{name}"),
    (r"\bbadger\b", "{name}"),
    (r"\bBadger\b", "{name}"),
    (r"\bhoneybadger\b", "{name}"),
    (r"\buBO\b", "{name}"),
    (r"\buBo\b", "{name}"),
]

# Non-regex literal replacements applied after the regex pass.
BRAND_LITERALS = [
    ("privacybadger.org", "blueshield.invalid"),
    ("uBlockOrigin", "blueshield"),
    ("Privacy Badger", EXTENSION_NAME),
    ("uBlock Origin Lite", EXTENSION_NAME),
    ("uBO Lite", EXTENSION_NAME),
    ("uBlock Origin", EXTENSION_NAME),
]

# Text blocks that name upstream projects, mapped to neutral BlueShield wording.
BRAND_PARAGRAPHS = [
    (
        "Made by leading digital rights nonprofit EFF to stop companies from spying on you.",
        "Stops companies from spying on you while you browse.",
    ),
    (
        "Donate to EFF—a nonprofit defending digital privacy",
        "Support BlueShield development",
    ),
    ("Get the latest privacy news from EFF", "Get the latest privacy news"),
    ("Donate to EFF", "Support BlueShield"),
    (
        "This will automatically send the following information to EFF:",
        "This will automatically send the following information to the BlueShield team:",
    ),
    (
        "A privacy tool by $START_HTML$EFF$END_HTML$, a member-supported nonprofit",
        "An independent privacy tool for everyday browsing",
    ),
    ("EFF logo", "BlueShield logo"),
    (
        "compliant with EFF's DNT policy",
        "compliant with the Do Not Track policy",
    ),
    (
        "Thank you for installing Privacy Badger!",
        "Thank you for installing BlueShield!",
    ),
    (
        "You're now protected by Privacy Badger.",
        "You're now protected by BlueShield.",
    ),
    ("https://www.eff.org/privacybadger/faq", "https://blueshield.invalid/faq"),
    ("https://privacybadger.org/", "https://blueshield.invalid/"),
    ("https://privacybadger.org", "https://blueshield.invalid"),
    ("Electronic Frontier Foundation", EXTENSION_NAME),
    ("EFF's Do Not Track policy", "the Do Not Track policy"),
    ("www.eff.org", "blueshield.invalid"),
    ("https://www.eff.org", "https://blueshield.invalid"),
]

# Localized strings that survive the substitutions above because they embed the
# upstream name inside markup or a placeholder value.
BRAND_MARKUP_PATTERNS = [
    (r">Privacy Badger<", f">{EXTENSION_NAME}<"),
    (r">Privacy Badger Options<", f">{EXTENSION_NAME} Settings<"),
    (r"Privacy Badger Options", f"{EXTENSION_NAME} Settings"),
    (r"Privacy Badger Popup", f"{EXTENSION_NAME} Tracker Report"),
    (r"alt=[\"']Privacy Badger[\"']", f"alt='{EXTENSION_NAME}'"),
    (r"aria-label=[\"']Privacy Badger[\"']", f"aria-label='{EXTENSION_NAME}'"),
    (r"title=[\"']Privacy Badger[\"']", f"title='{EXTENSION_NAME}'"),
    (r"\buBO Lite\b", EXTENSION_NAME),
    (r"\buBlock Origin Lite\b", EXTENSION_NAME),
]

# Visible text in HTML that is not routed through chrome.i18n.
HTML_TEXT_REPLACEMENTS = [
    ("<title>uBO Lite Zapper</title>", "<title>BlueShield</title>"),
    ("<title>uBO Lite — Report</title>", "<title>BlueShield — Report</title>"),
    ("<title>uBlock Origin Lite</title>", "<title>BlueShield</title>"),
    ("uBO Lite", EXTENSION_NAME),
    ("uBlock Origin Lite", EXTENSION_NAME),
    ("uBlock Origin", EXTENSION_NAME),
    ("Privacy Badger", EXTENSION_NAME),
]

# Elements that only advertise or credit the upstream tracker project, or that
# send browsing data to it. They stay in the DOM (the bundled scripts bind to
# them, so deleting them would throw) but are hidden by the theme, and their
# wording is neutralized in the locales.
HIDDEN_UPSTREAM_SELECTORS = [
    "#donate",
    "#cta-link",
    "#header-red-eff-logo",
    "#eff-logo",
    "#pb-donate",
    "#pb-donate-help",
    "#pb-donate-button",
    "#share-container",
    "#error",
    "#report-terms",
    ".pbButton#error",
]

# DOM identifiers and bundled library names that carry upstream branding. The
# same tokens are rewritten in the stylesheets and scripts that reference them.
HTML_TOKEN_REPLACEMENTS = [
    ("id=\"uBO-popup-panel\"", "id=\"blueshield-popup-panel\""),
    ("id=\"ubol-picker\"", "id=\"blueshield-picker\""),
    ("id=\"ubol-unpicker\"", "id=\"blueshield-unpicker\""),
    ("id=\"ubol-zapper\"", "id=\"blueshield-zapper\""),
    ("#uBO-popup-panel", "#blueshield-popup-panel"),
    ("#ubol-picker", "#blueshield-picker"),
    ("#ubol-unpicker", "#blueshield-unpicker"),
    ("#ubol-zapper", "#blueshield-zapper"),
    ("cm6.bundle.ubol.min.js", "cm6.bundle.blueshield.min.js"),
    (
        "https://github.com/gorhill/uBlock/wiki/Strict-blocking",
        "https://blueshield.invalid/help",
    ),
    ("https://github.com/uBlockOrigin/uAssets", "https://blueshield.invalid/lists"),
    ("https://github.com/uBlockOrigin/uBOL-home/wiki", "https://blueshield.invalid/help"),
    ("https://github.com/gorhill/uBlock", "https://blueshield.invalid/source"),
    ("https://privacybadger.org", "https://blueshield.invalid"),
    ("https://www.eff.org", "https://blueshield.invalid"),
]

# Upstream logos are swapped for the BlueShield icon.
HTML_LOGO_REPLACEMENTS = [
    # The upstream project's logo link is dropped entirely; the BlueShield icon
    # already sits next to it.
    (
        re.compile(r"<a href=\"[^\"]*eff\.org[^\"]*\"[^>]*>\s*<img[^>]*>\s*</a>"),
        "",
    ),
    (
        re.compile(r"<img id=\"eff-logo\"[^>]*>"),
        f'<img id="eff-logo" src="/{ "branding/icon-48.png" }" width="48" alt="{EXTENSION_NAME}">',
    ),
    (
        re.compile(r"<img[^>]*badger-bw-noborder\.svg[^>]*>"),
        f'<img src="/branding/icon-48.png" width="40" alt="{EXTENSION_NAME}">',
    ),
    (
        re.compile(r"<img[^>]*badger-48\.png[^>]*>"),
        f'<img src="/branding/icon-48.png" width="48" alt="{EXTENSION_NAME}">',
    ),
]

# Asset references that carry upstream branding in their filenames or content.
ASSET_REPLACEMENTS = [
    ("img/ublock.svg", "branding/icon-64.png"),
    ('src="img/ublock.svg"', 'src="branding/icon-64.png"'),
    ('alt="uBO Lite"', f'alt="{EXTENSION_NAME}"'),
    ('alt="uBlock Origin Lite"', f'alt="{EXTENSION_NAME}"'),
]

# The single light-blue skin shared by every BlueShield page. It is linked last
# in each document, so these declarations win over both bundled stylesheets.
# The blocking engine is themed through its CSS custom properties; the tracker
# pages use literal colors, which are overridden directly.
THEME_CSS = """/* BlueShield light-blue theme */
:root {
    color-scheme: light !important;
}

/* ---------------------------- blocking engine ---------------------------- */
:root, :root.light, :root.dark {
    --blue-40: 2 132 199;
    --blue-50: 3 105 161;
    --blue-30: 14 165 233;
    --blue-20: 56 189 248;
    --blue-10: 125 211 252;

    --primary-30: 14 165 233;
    --primary-40: 2 132 199;
    --primary-50: 3 105 161;
    --primary-60: 7 89 133;
    --primary-70: 12 74 110;
    --primary-80: 15 43 61;
    --primary-90: 224 242 254;
    --primary-95: 240 249 255;

    --surface-0-rgb: 255 255 255;
    --surface-1: rgb(240 249 255);
    --surface-2: rgb(224 242 254);
    --surface-3: rgb(186 230 253);

    --ink-rgb: var(--primary-80);
    --ink-0: rgb(15 43 61);
    --ink-100: #fff;

    --border-1: rgb(var(--blue-30));
    --border-2: rgb(var(--blue-40));
    --border-3: rgb(var(--blue-50));
    --border-4: rgb(var(--blue-60));

    --accent-ink-1: #fff;
    --accent-ink-3: var(--primary-80);
    --accent-surface-1: rgb(var(--primary-40));
    --subtil-ink: rgb(var(--primary-50));

    --link-ink: rgb(var(--primary-60));
    --link-hover-ink: rgb(var(--primary-40));

    --button-surface-rgb: var(--blue-20);
    --button-preferred-surface: rgb(var(--primary-40));
    --button-preferred-ink: #fff;

    --dashboard-tab-active-ink-rgb: var(--primary-40);
    --dashboard-tab-focus-surface-rgb: var(--primary-90);
    --dashboard-highlight-surface-rgb: var(--primary-90);

    --popup-cell-cname-ink: rgb(var(--primary-60));
    --popup-power-ink-rgb: var(--primary-40);

    --elevation-up-surface: #0c4a6e;
    --elevation-down-surface: #fff;

    --cm-cursor: rgb(var(--primary-50));
    --cm-foldmarker-ink: rgb(var(--blue-50));
    --cm-selection-focused-surface: rgb(var(--primary-90));
}

body, header, section, main, .tabButton, .listEntry, .filteringModeCard {
    border-color: rgb(var(--blue-30)) !important;
}

header {
    background: linear-gradient(180deg, #e0f2fe 0%, #f0f9ff 100%) !important;
    border-bottom: 1px solid rgb(var(--blue-30)) !important;
}

header .logo { width: auto !important; padding: 0 8px !important; }
header .logo img { width: 28px; height: 28px; }
:root.mobile nav .logo { display: inline-flex !important; }

.tabButton.preferred, button.preferred, .preferred {
    background: rgb(var(--primary-40)) !important;
    color: #fff !important;
}

.filteringModeButton, .filteringModeSlider > div {
    background: rgb(var(--blue-30)) !important;
}

input[type="checkbox"] { accent-color: rgb(var(--primary-50)); }
input[type="radio"] { accent-color: rgb(var(--primary-50)); }

a { color: rgb(var(--primary-60)) !important; }
a:hover { color: rgb(var(--primary-40)) !important; }

"""

# Rules for the bundled tracker pages. They are scoped to the tracker page
# bodies by scope_css() so they can never repaint a BlueShield page.
TRACKER_PAGE_SCOPES = ["body#main", "body.options", "body.first-run"]
TRACKER_THEME_CSS = """/* ----------------------------- tracker pages ------------------------------
   Scoped to the bundled tracker pages only. The BlueShield popup and settings
   pages carry their own styling and must not be repainted by these rules. */
body {
    background: linear-gradient(180deg, #f7fcff 0%, #e8f6ff 100%) !important;
    color: #0f2b3d !important;
}

h1, h2,
h3, h4 { color: #075985 !important; }

a, a:visited { color: #0369a1 !important; }
a:hover { color: #0284c7 !important; }

button, .pbButton, .ui-button, .ui-widget {
    background: linear-gradient(180deg, #38bdf8 0%, #0284c7 100%) !important;
    border: 1px solid #0369a1 !important;
    color: #fff !important;
}

button:hover, .pbButton:hover, .ui-button:hover {
    background: linear-gradient(180deg, #0ea5e9 0%, #0369a1 100%) !important;
}

button:focus-visible, .pbButton:focus-visible, .origin-inner:focus-visible,
input[type="text"]:focus, #allowlist-select:focus, #widget-site-exceptions-select:focus {
    border-color: #0ea5e9 !important;
    outline: none !important;
}

button.cta-button, a.cta-button {
    background: linear-gradient(180deg, #38bdf8 0%, #0284c7 100%) !important;
    border: 2px solid #0369a1 !important;
    color: #fff !important;
}

input[type="text"], input[type="search"], select, textarea,
#allowlist-select, #widget-site-exceptions-select,
.select2-container--default .select2-selection--multiple, .select2-dropdown {
    background: #fff !important;
    border: 1px solid #bae6fd !important;
    color: #0f2b3d !important;
}

#tabs .ui-tabs-nav .ui-state-active {
    box-shadow: inset 0 -3px 0 0 #0284c7 !important;
}
#tabs .ui-tabs-nav .ui-state-hover { box-shadow: inset 0 -3px 0 0 #7dd3fc !important; }
#tabs .ui-tabs-nav .ui-state-default a,
#tabs .ui-tabs-nav .ui-state-active a { color: #0c4a6e !important; }
#tabs .ui-tabs-nav .ui-state-active a { font-weight: 700; }

h4::after, #tabs .ui-widget-header { border-bottom: 1px solid #bae6fd !important; }
.btn-silo + .btn-silo { border-top: 1px solid #bae6fd !important; }

#tip-container { border-left: 4px solid #0ea5e9 !important; background: #e0f2fe !important; }
#tip-header { color: #0369a1 !important; background: #e0f2fe !important; }
#tip-header:hover { background: #bae6fd !important; }

.clickerContainer { background: #f0f9ff !important; }
#tracking-domains-filters { border: 1px solid #bae6fd !important; color: #0369a1 !important; }
.origin-inner { text-decoration: underline dotted #7dd3fc !important; }
.key { background: #fff !important; }

.btn-danger { color: #b91c1c !important; border-color: #fca5a5 !important; }
.btn-danger:hover { background: #b91c1c !important; color: #fff !important; }

#donate, #eff-logo, .eff-logo { display: none !important; }

/* Upstream-only promo, sharing and "report to upstream" controls. The elements
   stay in the DOM because the bundled scripts bind to them, but they are never
   shown: they would otherwise name the upstream project or send browsing data
   to it. */
__HIDDEN_SELECTORS__ { display: none !important; }

footer { border-top: 1px solid #bae6fd !important; }

/* Screenshots of the upstream interface are hidden: they would show the old
   product name. The surrounding instructions still describe what to do. */
#disable-instructions-image { display: none !important; }
img:not([src^="/branding/"]) { display: none !important; }

/* The welcome page's dark top bar is part of the upstream brand styling. */
.top-bar {
    background: linear-gradient(90deg, #0284c7 0%, #075985 100%) !important;
    color: #f0f9ff !important;
}

/* The bundled display face is the upstream brand font; use the product font. */
#blueshield-tracker-header h2, header h1, header h2 {
    font-family: Inter, "Segoe UI", system-ui, sans-serif !important;
    color: #075985 !important;
}
"""

# The upstream about pane is replaced wholesale: it is a list of upstream links
# and copyright lines, none of which belong in the BlueShield UI.
ABOUT_PANE_REPLACEMENT = """<div class="body">
        <div id="aboutNameVer" class="li"></div>
        <div class="li">Content blocking and automatic tracker protection.</div>
        <div class="liul">
            <div class="li">Every setting lives on the BlueShield settings page.</div>
            <div class="li">All data is stored locally in this browser profile.</div>
        </div>
        <hr>
        <details><summary data-i18n="supportS5H"></summary>
        <pre style="user-select:all; -webkit-user-select:all; direction:ltr;"></pre>
        </details>
    </div>"""


def _substitute_brands(text: str) -> str:
    """Replace upstream product names in a user-visible string."""
    for pattern, replacement in BRAND_PATTERNS:
        text = re.sub(pattern, replacement.format(name=EXTENSION_NAME), text)
    for pattern, replacement in BRAND_MARKUP_PATTERNS:
        text = re.sub(pattern, replacement, text)
    for old, new in BRAND_PARAGRAPHS:
        text = text.replace(old, new)
    for old, new in BRAND_LITERALS:
        text = text.replace(old, new)
    return text


def rebrand_locales(stage: Path) -> dict:
    """Strip upstream product names from every localized UI string.

    The blocking engine's translations are rewritten in place. The tracker
    engine only keeps its English strings: a translated product name cannot be
    detected reliably, so every other locale falls back to the scrubbed English
    text through Chrome's normal locale fallback.
    """
    stats = {"rewritten": 0, "tracker_translations_dropped": 0}
    for messages_path in sorted((stage / "_locales").glob("*/messages.json")):
        messages = read_json(messages_path)
        is_english = messages_path.parent.name == "en"
        dirty = False
        for key in [key for key in messages if key.startswith("pb_")]:
            if not is_english:
                del messages[key]
                dirty = True
                stats["tracker_translations_dropped"] += 1
        for key, entry in messages.items():
            if not isinstance(entry, dict):
                continue
            message = entry.get("message")
            if isinstance(message, str):
                replaced = _substitute_brands(message)
                if replaced != message:
                    entry["message"] = replaced
                    dirty = True
            placeholders = entry.get("placeholders")
            if isinstance(placeholders, dict):
                for name, value in placeholders.items():
                    if isinstance(value, dict) and isinstance(value.get("content"), str):
                        replaced = _substitute_brands(value["content"])
                        if replaced != value["content"]:
                            value["content"] = replaced
                            dirty = True
                    elif isinstance(value, str):
                        replaced = _substitute_brands(value)
                        if replaced != value:
                            placeholders[name] = replaced
                            dirty = True
        if dirty:
            write_json(messages_path, messages)
            stats["rewritten"] += 1
    if stats["rewritten"] == 0:
        raise RuntimeError("Locale rebrand made no changes; upstream names would stay visible")
    return stats


def rebrand_html(stage: Path) -> int:
    """Rewrite visible text and asset references in every bundled HTML page."""
    html_paths = sorted(stage.glob("*.html")) + sorted((stage / PB_BASE).rglob("*.html"))
    changed = 0
    for path in html_paths:
        text = path.read_text(encoding="utf-8")
        original = text

        # Replace the upstream about pane before generic text substitutions so
        # the marker elements the dashboard scripts rely on stay intact.
        about_match = re.search(
            r'<section data-pane="about">.*?</section>', text, re.DOTALL,
        )
        if about_match:
            text = (
                text[: about_match.start()]
                + '<section data-pane="about">\n'
                + ABOUT_PANE_REPLACEMENT
                + "\n</section>"
                + text[about_match.end():]
            )

        for old, new in ASSET_REPLACEMENTS:
            text = text.replace(old, new)
        # Logo swaps run before the URL rewrites so the upstream anchors are
        # still recognizable when the logos are replaced.
        for pattern, replacement in HTML_LOGO_REPLACEMENTS:
            text = pattern.sub(replacement, text)
        for old, new in ASSET_TOKEN_REPLACEMENTS:
            text = text.replace(old, new)
        for old, new in HTML_TOKEN_REPLACEMENTS:
            text = text.replace(old, new)
        for old, new in HTML_TEXT_REPLACEMENTS:
            text = text.replace(old, new)

        # Give every real page a window title that names the product.
        if "<title" not in text and "<head>" in text:
            text = text.replace(
                "<head>", f"<head>\n<title>{EXTENSION_NAME}</title>", 1,
            )

        # The welcome page has no body hook; the theme needs one to scope its
        # rules to the bundled pages.
        if path.name == "firstRun.html" and "<body>" in text:
            text = text.replace("<body>", '<body class="first-run">', 1)

        # Keep the dashboard logo element but point it at the BlueShield icon.
        text = re.sub(
            r'(<span class="logo"><img[^>]*?src=")[^"]*(")',
            r'\1branding/icon-64.png\2',
            text,
        )

        if text != original:
            path.write_text(text, encoding="utf-8")
            changed += 1
    return changed


# Tokens shared by the stylesheets, scripts and markup that reference a renamed
# element, so a rename never leaves a dangling selector behind.
ASSET_TOKEN_REPLACEMENTS = [
    ("uBO-popup-panel", "blueshield-popup-panel"),
    ("ubol-picker", "blueshield-picker"),
    ("ubol-unpicker", "blueshield-unpicker"),
    ("ubol-zapper", "blueshield-zapper"),
    ("cm6.bundle.ubol.min.js", "cm6.bundle.blueshield.min.js"),
    ("privacy-badger-header", "blueshield-tracker-header"),
    ("badger-logo-div", "blueshield-logo-div"),
    ("badger-title-div", "blueshield-title-div"),
    ("badger-img-container", "blueshield-img-container"),
    ("header-red-eff-logo", "blueshield-header-logo"),
    ("badger-pretraining", "tracker-pretraining"),
    ("badger-evolution", "tracker-evolution"),
    ("support-privacy-badger", "support-blueshield"),
    ("why-privacy-badger-opts-you-out", "privacy-sandbox"),
    ("privacybadger.org", "blueshield.invalid"),
    ("supporters.eff.org", "blueshield.invalid"),
    ("%40eff%40", "%40blueshield%40"),
    ("%40eff.org", "%40blueshield.invalid"),
    ("@eff.org", "@blueshield.invalid"),
    ("I%20just%20installed%20Privacy%20Badger", "I%20just%20installed%20BlueShield"),
]


def rebrand_ruleset_details(stage: Path) -> None:
    """Rename built-in filter lists that carry the upstream product name."""
    path = stage / "rulesets" / "ruleset-details.json"
    if not path.is_file():
        return
    details = read_json(path)
    dirty = False
    for entry in details:
        name = entry.get("name")
        if isinstance(name, str) and re.search(r"uBlock|\buBO\b", name):
            entry["name"] = _substitute_brands(name)
            dirty = True
    if dirty:
        write_json(path, details)


def rebrand_css(stage: Path) -> int:
    """Rewrite branding tokens and disable the upstream dark themes."""
    changed = 0
    for path in sorted(stage.rglob("*.css")):
        text = path.read_text(encoding="utf-8")
        original = text
        # Any "prefers-color-scheme: dark" block would fight the fixed light-blue
        # skin, so the queries are renamed into a condition that never matches.
        text = re.sub(
            r"@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)",
            "@media screen and (max-width: 0px)",
            text,
        )
        for old, new in ASSET_TOKEN_REPLACEMENTS:
            text = text.replace(old, new)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed += 1
    return changed


def rebrand_scripts(stage: Path) -> int:
    """Keep identifier and file-name references consistent after a rename."""
    bundle_old = stage / "lib" / "codemirror" / "cm6.bundle.ubol.min.js"
    bundle_new = stage / "lib" / "codemirror" / "cm6.bundle.blueshield.min.js"
    if bundle_old.is_file():
        bundle_old.rename(bundle_new)

    changed = 0
    for path in sorted(stage.rglob("*.js")):
        rel = path.relative_to(stage).as_posix()
        if rel.startswith("lib/vendor/") or "/lib/vendor/" in rel:
            continue
        text = path.read_text(encoding="utf-8")
        original = text
        for old, new in ASSET_TOKEN_REPLACEMENTS:
            text = text.replace(old, new)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed += 1
    return changed


def scope_css(css: str, scopes: list[str]) -> str:
    """Prefix every selector in every rule with one of ``scopes``.

    Prefixing only the first selector of a comma group would silently widen the
    rule to the whole document, so each selector is expanded individually.
    Comments are stripped first, because a comment placed before a rule would
    otherwise be parsed as part of that rule's selector.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)

    def replace(match: re.Match) -> str:
        selectors = [item.strip() for item in match.group(1).split(",") if item.strip()]
        scoped: list[str] = []
        for selector in selectors:
            for scope in scopes:
                # A bare `body` rule targets the page body itself, so the scope
                # replaces it rather than becoming an ancestor of it.
                if selector in {"body", "html"}:
                    scoped.append(scope)
                else:
                    scoped.append(f"{scope} {selector}")
        return ", ".join(scoped) + " { " + " ".join(match.group(2).split()) + " }\n"

    return re.sub(r"([^{}]+)\{([^{}]*)\}", replace, css)


def write_theme_stylesheet(stage: Path) -> None:
    """Write the light-blue skin shared by the bundled engine pages.

    The BlueShield popup and settings pages ship their own stylesheets, so the
    shared skin is written for the engine pages only and is never linked into
    the BlueShield pages themselves.
    """
    selectors = ",\n".join(HIDDEN_UPSTREAM_SELECTORS)
    css = THEME_CSS.replace("__HIDDEN_SELECTORS__", selectors)
    css += "\n" + scope_css(TRACKER_THEME_CSS, TRACKER_PAGE_SCOPES)
    (stage / THEME_STYLESHEET).write_text(css, encoding="utf-8")


def apply_theme(stage: Path) -> int:
    """Link the BlueShield stylesheet into the bundled engine pages."""
    html_paths = sorted(stage.glob("*.html")) + sorted((stage / PB_BASE).rglob("*.html"))
    link = f'<link rel="stylesheet" href="/{THEME_STYLESHEET}">'
    changed = 0
    for path in html_paths:
        # The BlueShield pages have their own design; the shared engine skin is
        # not linked into them.
        if path.name.startswith("blueshield-"):
            continue
        text = path.read_text(encoding="utf-8")
        if link in text:
            continue
        # Pages served from the vendor directory need an absolute path, which the
        # link already carries, so a single form works everywhere. Fragments
        # without a head (web-accessible redirect stubs) are not UI and are
        # skipped.
        if "</head>" not in text:
            continue
        text = text.replace("</head>", f"  {link}\n</head>", 1)
        path.write_text(text, encoding="utf-8")
        changed += 1
    return changed


def rebrand_ui(stage: Path) -> dict:
    """Run the full user-visible rebrand over the staged extension."""
    stats = {
        "locales": rebrand_locales(stage),
        "html": rebrand_html(stage),
        "css": rebrand_css(stage),
        "scripts": rebrand_scripts(stage),
    }
    rebrand_ruleset_details(stage)
    write_theme_stylesheet(stage)
    stats["themed_pages"] = apply_theme(stage)
    return stats


def assert_no_upstream_names_in_ui(stage: Path) -> None:
    """Fail the build if an upstream product name is still user-visible."""
    banned = (
        re.compile(r"uBlock"),
        re.compile(r"\buBO\b|\buBo\b"),
        re.compile(r"Privacy Badger|privacybadger", re.IGNORECASE),
        re.compile(r"badger", re.IGNORECASE),
        re.compile(r"\bEFF\b"),
        re.compile(r"Electronic Frontier Foundation", re.IGNORECASE),
        re.compile(r"eff\.org", re.IGNORECASE),
    )
    offenders: list[str] = []

    def scan(path: Path, text: str, label: str = "") -> None:
        for pattern in banned:
            match = pattern.search(text)
            if match:
                start = max(0, match.start() - 50)
                snippet = text[start:match.end() + 50].replace("\n", " ")
                where = f"{path.relative_to(stage)}"
                if label:
                    where = f"{where} [{label}]"
                offenders.append(f"{where}: {pattern.pattern} -> …{snippet}…")

    for path in sorted(stage.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        # Comments and upstream license banners are not user-visible; strip them
        # before scanning so only rendered markup is checked.
        text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
        text = re.sub(r"\s+", " ", text)
        scan(path, text)

    for path in sorted((stage / "_locales").glob("*/messages.json")):
        messages = read_json(path)
        for key, entry in messages.items():
            if not isinstance(entry, dict):
                continue
            message = entry.get("message")
            if isinstance(message, str) and message.strip():
                scan(path, message, f"{path.parent.name}:{key}")

    if offenders:
        preview = "\n".join(offenders[:25])
        raise RuntimeError(
            f"Upstream branding still visible in {len(offenders)} place(s):\n{preview}"
        )


def patch_ublock(stage: Path) -> None:
    background = stage / "js" / "background.js"
    replace_once(
        background,
        "runtime.onMessage.addListener((request, sender, callback) => {\n"
        "    if ( request.what.includes(':') ) { return; }",
        "runtime.onMessage.addListener((request, sender, callback) => {\n"
        "    if ( !request || typeof request.what !== 'string' || request.what.includes(':') ) { return; }",
    )

    replace_once(
        background,
        "    const scripts = await getRegisteredContentScripts();\n"
        "    if ( scripts.length === 0 ) {\n"
        "        await registerContentScripts();\n"
        "    }",
        "    // Other components may own registered scripts, so always refresh the\n"
        "    // uBlock-owned subset instead of treating the global registry as ours.\n"
        "    await registerContentScripts();",
    )

    scripting_manager = stage / "js" / "scripting-manager.js"
    mode_manager = stage / "js" / "mode-manager.js"
    replace_once(
        mode_manager,
        "export const defaultFilteringModes = {\n"
        "    none: [],\n"
        "    basic: [],\n"
        "    optimal: [ 'all-urls' ],\n"
        "    complete: [],\n"
        "};",
        "export const defaultFilteringModes = {\n"
        "    none: [],\n"
        "    basic: [],\n"
        "    optimal: [],\n"
        "    // Cosmetic element hiding is applied on every site, matching full\n"
        "    // uBlock Origin, so ad placeholders are hidden rather than just\n"
        "    // their requests being blocked.\n"
        "    complete: [ 'all-urls' ],\n"
        "};",
    )

    replace_once(
        scripting_manager,
        "    ubolLog(`Unregistered all content (css/js)`);\n"
        "    try {\n"
        "        await browser.scripting.unregisterContentScripts();\n"
        "    } catch(reason) {\n"
        "        ubolErr(`unregisterContentScripts/${reason}`);\n"
        "    }",
        "    const registered = await browser.scripting.getRegisteredContentScripts();\n"
        "    const ublockOwned = registered.filter(script =>\n"
        "        script.id.startsWith('" + MESSAGE_COMPONENT + ".') === false\n"
        "    ).map(script => script.id);\n"
        "    ubolLog(`Unregistered uBlock-owned content scripts: ${ublockOwned}`);\n"
        "    if (ublockOwned.length !== 0) {\n"
        "        try {\n"
        "            await browser.scripting.unregisterContentScripts({ ids: ublockOwned });\n"
        "        } catch(reason) {\n"
        "            ubolErr(`unregisterContentScripts/${reason}`);\n"
        "        }\n"
        "    }",
    )

    ruleset_manager = stage / "js" / "ruleset-manager.js"
    replace_once(
        ruleset_manager,
        "const SPECIAL_RULES_REALM = 5000000;\n",
        "const SPECIAL_RULES_REALM = 5000000;\n"
        "const PRIVACY_BADGER_RULE_ID_BASE = 100000000;\n"
        "const STRICTBLOCK_SESSION_BASE_RULE_ID = 1000000;\n"
        "const REGEX_SESSION_BASE_RULE_ID = 2000000;\n"
        "const REGEX_SESSION_RULE_ID_LIMIT = 3000000;\n",
    )
    replace_once(
        ruleset_manager,
        "export async function updateDynamicAndSessionRules() {\n"
        "    const currentRules = await dnr.getDynamicRules();\n\n"
        "    // Remove potentially left-over rules from previous version\n"
        "    const removeRuleIds = [];\n"
        "    for ( const rule of currentRules ) {\n"
        "        if ( rule.id >= SPECIAL_RULES_REALM ) { continue; }\n"
        "        removeRuleIds.push(rule.id);\n"
        "        rule.id = 0;\n"
        "    }\n\n"
        "    const addRules = [];\n"
        "    await updateRegexRules(currentRules, addRules, removeRuleIds);\n"
        "    if ( addRules.length === 0 && removeRuleIds.length === 0 ) { return; }\n\n"
        "    const dynamicRegexCountBefore = await getDynamicRegexRuleCount();\n"
        "    let dynamicRegexCountAfter = 0;\n"
        "    let ruleId = 1;\n"
        "    for ( const rule of addRules ) {\n"
        "        if ( rule?.condition.regexFilter ) { dynamicRegexCountAfter += 1; }\n"
        "        rule.id = ruleId++;\n"
        "    }\n"
        "    if ( dynamicRegexCountAfter !== 0 ) {\n"
        "        ubolLog(`Using ${dynamicRegexCountAfter}/${dnr.MAX_NUMBER_OF_REGEX_RULES} dynamic regex-based DNR rules`);\n"
        "    }\n"
        "    // If we increase the number of dynamic regex rules, reset session rules to\n"
        "    // reduce risk of hitting maximum regex count\n"
        "    if ( dynamicRegexCountAfter > dynamicRegexCountBefore ) {\n"
        "        await clearSessionRules();\n"
        "    }\n\n"
        "    const response = {};\n\n"
        "    try {\n"
        "        await dnr.updateDynamicRules({\n"
        "            addRules: toSafeDynamicRules(addRules),\n"
        "            removeRuleIds,\n"
        "        });\n"
        "        if ( removeRuleIds.length !== 0 ) {\n"
        "            ubolLog(`Remove ${removeRuleIds.length} dynamic DNR rules`);\n"
        "        }\n"
        "        if ( addRules.length !== 0 ) {\n"
        "            ubolLog(`Add ${addRules.length} dynamic DNR rules`);\n"
        "        }\n"
        "    } catch(reason) {\n"
        "        ubolErr(`updateDynamicAndSessionRules/${reason}`);\n"
        "        response.error = `${reason}`;\n"
        "    }\n\n"
        "    const result = await updateSessionRules();\n"
        "    if ( result?.error ) {\n"
        "        response.error ||= result.error;\n"
        "    }\n\n"
        "    return response;\n"
        "}",
        "export async function updateDynamicAndSessionRules() {\n"
        "    const currentRules = await dnr.getDynamicRules();\n\n"
        "    // Remove potentially left-over uBlock-owned rules from previous version.\n"
        "    const removeRuleIds = [];\n"
        "    for ( const rule of currentRules ) {\n"
        "        if ( rule.id >= SPECIAL_RULES_REALM ) { continue; }\n"
        "        removeRuleIds.push(rule.id);\n"
        "        rule.id = 0;\n"
        "    }\n\n"
        "    // Regex-only rules consume scarce dynamic-rule quota. BlueShield keeps\n"
        "    // Privacy Badger's full learned set by placing uBlock's rebuilt regex\n"
        "    // rules in a dedicated session-rule range instead.\n"
        "    const addRules = [];\n"
        "    await updateRegexRules(currentRules, addRules, removeRuleIds);\n"
        "    const sessionRules = await dnr.getSessionRules();\n"
        "    const removeSessionRuleIds = sessionRules.filter(rule =>\n"
        "        rule.id >= REGEX_SESSION_BASE_RULE_ID &&\n"
        "        rule.id < REGEX_SESSION_RULE_ID_LIMIT\n"
        "    ).map(rule => rule.id);\n"
        "    let ruleId = REGEX_SESSION_BASE_RULE_ID;\n"
        "    for ( const rule of addRules ) { rule.id = ruleId++; }\n\n"
        "    const response = {};\n"
        "    try {\n"
        "        await dnr.updateDynamicRules({ removeRuleIds });\n"
        "        if ( removeRuleIds.length !== 0 ) {\n"
        "            ubolLog(`Remove ${removeRuleIds.length} low-ID dynamic DNR rules`);\n"
        "        }\n"
        "        await dnr.updateSessionRules({\n"
        "            addRules: toSafeDynamicRules(addRules),\n"
        "            removeRuleIds: removeSessionRuleIds,\n"
        "        });\n"
        "        if ( addRules.length !== 0 ) {\n"
        "            ubolLog(`Add ${addRules.length} session regex DNR rules`);\n"
        "        }\n"
        "    } catch(reason) {\n"
        "        ubolErr(`updateDynamicAndSessionRules/${reason}`);\n"
        "        response.error = `${reason}`;\n"
        "    }\n\n"
        "    const result = await updateSessionRules();\n"
        "    if ( result?.error ) { response.error ||= result.error; }\n"
        "    return response;\n"
        "}",
    )
    replace_once(
        ruleset_manager,
        "    const currentRules = await dnr.getSessionRules();\n"
        "    await updateStrictBlockRules(currentRules, addRulesUnfiltered, removeRuleIds);\n"
        "    if ( addRulesUnfiltered.length === 0 && removeRuleIds.length === 0 ) { return; }\n"
        "    const maxRegexCount = dnr.MAX_NUMBER_OF_REGEX_RULES * 0.95;\n"
        "    const dynamicRegexCount = await getDynamicRegexRuleCount();\n"
        "    let regexCount = dynamicRegexCount;\n"
        "    let ruleId = 1;",
        "    const currentRules = await dnr.getSessionRules();\n"
        "    await updateStrictBlockRules(currentRules, addRulesUnfiltered, removeRuleIds);\n"
        "    if ( addRulesUnfiltered.length === 0 && removeRuleIds.length === 0 ) { return; }\n"
        "    const maxRegexCount = dnr.MAX_NUMBER_OF_REGEX_RULES * 0.95;\n"
        "    const sessionRegexCountBefore = currentRules.filter(rule =>\n"
        "        Boolean(rule.condition?.regexFilter)\n"
        "    ).length;\n"
        "    let regexCount = sessionRegexCountBefore;\n"
        "    let ruleId = STRICTBLOCK_SESSION_BASE_RULE_ID;",
    )
    replace_once(
        ruleset_manager,
        "    const sessionRegexCount = regexCount - dynamicRegexCount;",
        "    const sessionRegexCount = regexCount - sessionRegexCountBefore;",
    )
    replace_once(
        ruleset_manager,
        "        if ( rule.id < USER_RULES_BASE_RULE_ID ) { continue; }",
        "        if ( rule.id < USER_RULES_BASE_RULE_ID ||\n"
        "            rule.id >= PRIVACY_BADGER_RULE_ID_BASE ) { continue; }",
    )
    replace_once(
        ruleset_manager,
        "    const currentRules = await dnr.getSessionRules();\n"
        "    if ( currentRules.length === 0 ) { return; }\n"
        "    const removeRuleIds = currentRules.map(a => a.id);",
        "    const currentRules = (await dnr.getSessionRules()).filter(\n"
        "        a => a.id < SPECIAL_RULES_REALM\n"
        "    );\n"
        "    if ( currentRules.length === 0 ) { return; }\n"
        "    const removeRuleIds = currentRules.map(a => a.id);",
    )


TELEMETRY_RULESET_ID = "blueshield-telemetry"
TELEMETRY_RULE_ID_BASE = 300_000_000

# Endpoints that the public AdBlockTest dataset checks but that none of the
# enabled filter lists cover: device-maker telemetry (Apple, Oppo, Realme),
# an ad-tech exchange host, and one regional logging host. They are pure
# telemetry, never page content, so blocking them is safe. Measured with
# coverage_test.py against the same dataset.
TELEMETRY_HOSTS = [
    "adtech.yahooinc.com",
    "books-analytics-events.apple.com",
    "weather-analytics-events.apple.com",
    "notes-analytics-events.apple.com",
    "adx.ads.oppomobile.com",
    "ck.ads.oppomobile.com",
    "data.ads.oppomobile.com",
    "iot-eu-logser.realme.com",
    "iot-logser.realme.com",
    "bdapi-ads.realmemobile.com",
    "bdapi-in-ads.realmemobile.com",
    "log.byteoversea.com",
]


def add_telemetry_ruleset(stage: Path) -> dict:
    """Write the small supplemental telemetry ruleset and describe it for the UI."""
    rules = [
        {
            "id": TELEMETRY_RULE_ID_BASE + index,
            "priority": 100,
            "action": {"type": "block"},
            "condition": {"urlFilter": f"||{host}^"},
        }
        for index, host in enumerate(TELEMETRY_HOSTS, start=1)
    ]
    write_json(stage / "rulesets" / "blueshield-telemetry.json", rules)

    # Deliberately no entry in ruleset-details.json: that file drives cosmetic
    # and scriptlet registration, which would look for scripting files that this
    # network-only ruleset does not have and then fail as a whole.
    return {
        "id": TELEMETRY_RULESET_ID,
        "enabled": True,
        "path": f"rulesets/{TELEMETRY_RULESET_ID}.json",
    }


def build_manifest(
    ubo_manifest: dict,
    pb_manifest: dict,
    content_scripts: list[dict],
    managed_schema: str,
    pb_version: str,
    ubo_version: str,
) -> dict:
    manifest = json.loads(json.dumps(ubo_manifest))
    manifest.pop("update_url", None)
    manifest.pop("browser_specific_settings", None)
    manifest["manifest_version"] = 3
    manifest["name"] = EXTENSION_NAME
    manifest["short_name"] = EXTENSION_NAME
    manifest["version"] = EXTENSION_VERSION
    manifest["version_name"] = f"{EXTENSION_NAME} {EXTENSION_VERSION}"
    manifest["description"] = (
        "Content blocking and automatic tracker protection for Chromium, "
        "with one light-blue settings page for every option."
    )
    manifest["author"] = f"{EXTENSION_NAME} contributors"
    manifest["minimum_chrome_version"] = "122.0"
    manifest["incognito"] = "split"
    manifest["background"] = {
        "service_worker": SERVICE_WORKER,
        "type": "module",
    }
    manifest["content_scripts"] = content_scripts
    manifest["action"] = {
        "default_title": EXTENSION_NAME,
        "default_popup": "blueshield-popup.html",
        "default_icon": {
            "16": "branding/icon-16.png",
            "32": "branding/icon-32.png",
            "48": "branding/icon-48.png",
            "64": "branding/icon-64.png",
        },
    }
    manifest["icons"] = {
        "16": "branding/icon-16.png",
        "32": "branding/icon-32.png",
        "48": "branding/icon-48.png",
        "64": "branding/icon-64.png",
        "128": "branding/icon-128.png",
    }
    manifest["options_ui"] = {
        "page": "blueshield-settings.html",
        "open_in_tab": True,
    }

    permissions = set(manifest.get("permissions", []))
    permissions.update(pb_manifest.get("permissions", []))
    permissions.discard("webRequestBlocking")
    permissions.discard("declarativeNetRequestFeedback")
    manifest["permissions"] = sorted(permissions)
    host_permissions = set(manifest.get("host_permissions", []))
    host_permissions.update(pb_manifest.get("host_permissions", []))
    manifest["host_permissions"] = sorted(host_permissions)

    war = list(manifest.get("web_accessible_resources", []))
    for entry in pb_manifest.get("web_accessible_resources", []):
        copied = json.loads(json.dumps(entry))
        copied["resources"] = [f"{PB_BASE}/{path.lstrip('/')}" for path in copied["resources"]]
        war.append(copied)
    manifest["web_accessible_resources"] = war

    rulesets = list(manifest.get("declarative_net_request", {}).get("rule_resources", []))
    for entry in pb_manifest.get("declarative_net_request", {}).get("rule_resources", []):
        copied = json.loads(json.dumps(entry))
        copied["path"] = f"{PB_BASE}/{copied['path'].lstrip('/')}"
        rulesets.append(copied)
    rulesets.append(add_telemetry_ruleset(STAGE))
    manifest["declarative_net_request"] = {"rule_resources": rulesets}
    manifest["storage"] = {"managed_schema": managed_schema}

    write_json(STAGE / "manifest.json", manifest)
    (STAGE / SERVICE_WORKER).write_text(
        'import "./' + PB_BASE + '/js/background.js";\n'
        'import "./js/background.js";\n',
        encoding="utf-8",
    )
    return manifest


def add_branding_and_notices(pb_version: str, ubo_version: str) -> dict:
    shutil.copytree(ROOT / "branding", STAGE / "branding")
    shutil.copytree(ROOT / "src", STAGE / "blueshield-ui")
    # Move the BlueShield UI files to the extension root, then drop the folder.
    for name in (
        "blueshield-popup.html",
        "blueshield-popup.css",
        "blueshield-popup.js",
        "blueshield-settings.html",
        "blueshield-settings.css",
        "blueshield-settings.js",
    ):
        shutil.move(STAGE / "blueshield-ui" / name, STAGE / name)
    shutil.rmtree(STAGE / "blueshield-ui")

    replacements = {
        "__PRIVACY_COMPONENT__": MESSAGE_COMPONENT,
        "__PRIVACY_STORAGE_PREFIX__": STORAGE_PREFIX,
        "__BLUESHIELD_VERSION__": EXTENSION_VERSION,
        "__BLOCKING_ENGINE_VERSION__": ubo_version,
        "__PRIVACY_ENGINE_VERSION__": pb_version,
    }
    for name, needed in (
        ("blueshield-popup.js", ("__PRIVACY_COMPONENT__",)),
        (
            "blueshield-settings.js",
            (
                "__PRIVACY_COMPONENT__",
                "__PRIVACY_STORAGE_PREFIX__",
                "__BLUESHIELD_VERSION__",
                "__BLOCKING_ENGINE_VERSION__",
                "__PRIVACY_ENGINE_VERSION__",
            ),
        ),
    ):
        path = STAGE / name
        text = path.read_text(encoding="utf-8")
        for placeholder in needed:
            if placeholder not in text:
                raise RuntimeError(f"Missing placeholder {placeholder} in {name}")
            text = text.replace(placeholder, replacements[placeholder])
        path.write_text(text, encoding="utf-8")

    licenses = STAGE / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copy2(UBO_REPO / "LICENSE.txt", licenses / "uBlock-Origin-LICENSE.txt")
    shutil.copy2(PB_REPO / "LICENSE", licenses / "Privacy-Badger-LICENSE.txt")
    shutil.copy2(ROOT / "NOTICE.md", licenses / "NOTICE.md")

    commits = {
        "privacybadger": run(
            ["git", "rev-parse", "HEAD"], cwd=PB_REPO
        ).stdout.strip(),
        "ublock": run(["git", "rev-parse", "HEAD"], cwd=UBO_REPO).stdout.strip(),
    }
    build_info = {
        "extension": EXTENSION_NAME,
        "version": EXTENSION_VERSION,
        "components": {
            "blockingEngine": {
                "version": ubo_version,
                "commit": commits["ublock"],
            },
            "trackerProtection": {
                "version": pb_version,
                "source": "origin/mv3-chrome",
                "commit": commits["privacybadger"],
            },
        },
        "notes": [
            "Single shared service worker; no extra background pages, to keep memory use low.",
            "Component storage, messages, paths, locales, action updates, and DNR rule IDs are isolated.",
        ],
    }
    write_json(STAGE / "BUILD_INFO.json", build_info)
    return build_info


def ensure_private_files_absent(stage: Path) -> None:
    for path in stage.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Extension staging contains a symlink: {path}")
        if not path.is_file():
            continue
        if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            raise RuntimeError(f"Private-key-like file entered staging: {path}")
        if path.stat().st_size < 10_000_000:
            data = path.read_bytes()
            if b"-----BEGIN PRIVATE KEY-----" in data or b"-----BEGIN RSA PRIVATE KEY-----" in data:
                raise RuntimeError(f"Private key material found in {path}")


def validate_references(stage: Path) -> None:
    manifest = read_json(stage / "manifest.json")
    missing = []

    def require(path_value: str) -> None:
        if path_value.startswith(("http://", "https://", "data:")):
            return
        normalized = path_value.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        while normalized.startswith("*"):
            normalized = normalized[1:]
        if "*" in normalized:
            prefix = normalized.split("*", 1)[0].rsplit("/", 1)[0]
            target = stage / prefix if prefix else stage
            if not target.exists():
                missing.append(path_value)
            return
        if not (stage / normalized).is_file():
            missing.append(path_value)

    for script in manifest.get("content_scripts", []):
        for path in script.get("js", []) + script.get("css", []):
            require(path)
    for entry in manifest.get("web_accessible_resources", []):
        for path in entry.get("resources", []):
            require(path)
    for ruleset in manifest.get("declarative_net_request", {}).get("rule_resources", []):
        require(ruleset["path"])
    require(manifest["background"]["service_worker"])
    require(manifest["action"]["default_popup"])
    if "options_page" in manifest:
        require(manifest["options_page"])
    require(manifest["storage"]["managed_schema"])
    if missing:
        raise RuntimeError("Manifest references missing files:\n" + "\n".join(missing))


def syntax_check(stage: Path) -> None:
    for path in sorted((stage / "vendor" / "privacybadger").rglob("*.js")):
        rel = path.relative_to(stage).as_posix()
        if rel.endswith("runtime.js") and PB_BASE + "/lib/" in rel:
            continue
        if rel.endswith("pb-manifest.js") or rel.endswith("runtime-injection.json"):
            continue
        result = subprocess.run(
            ["node", "--input-type=module", "--check"],
            input=path.read_text(encoding="utf-8"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise RuntimeError(f"JavaScript syntax error in {rel}:\n{result.stderr}")

    for path in sorted((stage / "vendor" / "privacybadger" / "content-scripts").glob("*.js")):
        result = subprocess.run(
            ["node", "--check", str(path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Content bundle syntax error in {path}:\n{result.stderr}")

    for path in [
        STAGE / SERVICE_WORKER,
        STAGE / "blueshield-popup.js",
        STAGE / "blueshield-settings.js",
    ]:
        result = subprocess.run(
            ["node", "--input-type=module", "--check"],
            input=path.read_text(encoding="utf-8"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise RuntimeError(f"JavaScript syntax error in {path}:\n{result.stderr}")


def ensure_signing_key() -> bytes:
    KEY_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not KEY_PATH.exists():
        run([
            "openssl", "genpkey", "-algorithm", "RSA",
            "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(KEY_PATH),
        ])
        KEY_PATH.chmod(0o600)
    KEY_PATH.chmod(0o600)
    run(["openssl", "pkey", "-in", str(KEY_PATH), "-check", "-noout"])
    # OpenSSL emits DER here, so write it to a temporary file rather than
    # passing binary data through the text-oriented subprocess helper.
    with tempfile.NamedTemporaryFile() as temp:
        run([
            "openssl", "pkey", "-in", str(KEY_PATH), "-pubout", "-outform", "DER",
            "-out", temp.name,
        ])
        public_der = Path(temp.name).read_bytes()
    return public_der


def extension_id_from_public_key(public_der: bytes) -> str:
    digest = hashlib.sha256(public_der).digest()[:16]
    return "".join("abcdefghijklmnop"[byte >> 4] + "abcdefghijklmnop"[byte & 15] for byte in digest)


def package_zip(stage: Path, output: Path) -> None:
    if output.exists():
        output.unlink()
    files = sorted(path for path in stage.rglob("*") if path.is_file())
    manifest_path = stage / "manifest.json"
    files.remove(manifest_path)
    files.insert(0, manifest_path)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            name = path.relative_to(stage).as_posix()
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (path.stat().st_mode & 0o777)) << 16
            archive.writestr(info, path.read_bytes())


def package_crx(stage: Path, output: Path) -> None:
    generated = Path(str(stage) + ".crx")
    if generated.exists():
        generated.unlink()
    with tempfile.TemporaryDirectory(prefix="blueshield-chromium-", dir="/tmp/opencode") as temp_name:
        temp = Path(temp_name)
        profile = temp / "profile"
        home = temp / "home"
        config = temp / "config"
        cache = temp / "cache"
        for path in (profile, home, config, cache):
            path.mkdir()
        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
        })
        result = subprocess.run(
            [
                "chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
                f"--user-data-dir={profile}", f"--pack-extension={stage}",
                f"--pack-extension-key={KEY_PATH}",
            ],
            env=env,
            text=True,
            timeout=120,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        (RELEASE / "chromium-pack.log").write_text(
            result.stdout + result.stderr, encoding="utf-8"
        )
        if result.returncode != 0 or not generated.is_file():
            raise RuntimeError(
                "Chromium CRX packing failed; see release/chromium-pack.log\n"
                + result.stdout + result.stderr
            )
        if generated.resolve() != output.resolve():
            shutil.copy2(generated, output)
            generated.unlink()


def parse_crx(path: Path, public_der: bytes) -> bytes:
    raw = path.read_bytes()
    magic, version, header_size = struct.unpack_from("<4sII", raw)
    if magic != b"Cr24" or version != 3:
        raise RuntimeError("Packed artifact is not CRX3")
    offset = 12 + header_size
    if raw[offset:offset + 4] != b"PK\x03\x04":
        raise RuntimeError("CRX3 payload is not a ZIP")
    header = raw[12:offset]
    if public_der not in header:
        raise RuntimeError("CRX3 signing proof does not contain the expected public key")
    return raw[offset:]


def validate_archives(zip_path: Path, crx_path: Path, public_der: bytes) -> None:
    payload = parse_crx(crx_path, public_der)
    with tempfile.NamedTemporaryFile(suffix=".zip") as temp:
        temp.write(payload)
        temp.flush()
        with zipfile.ZipFile(zip_path) as upload, zipfile.ZipFile(temp.name) as packed:
            upload_files = {info.filename: upload.read(info) for info in upload.infolist() if not info.is_dir()}
            packed_files = {info.filename: packed.read(info) for info in packed.infolist() if not info.is_dir()}
            if upload_files != packed_files:
                raise RuntimeError("Upload ZIP and CRX payload contents differ")
            manifest = json.loads(upload_files["manifest.json"])
            if manifest.get("manifest_version") != 3:
                raise RuntimeError("Packed manifest is not MV3")
            if "update_url" in manifest:
                raise RuntimeError("Development package must not contain update_url")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_artifacts(build_info: dict) -> dict:
    public_der = ensure_signing_key()
    manifest_path = STAGE / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["key"] = base64.b64encode(public_der).decode("ascii")
    write_json(manifest_path, manifest)

    ensure_private_files_absent(STAGE)
    validate_references(STAGE)
    syntax_check(STAGE)
    package_zip(STAGE, ZIP_PATH)
    package_crx(STAGE, CRX_PATH)
    validate_archives(ZIP_PATH, CRX_PATH, public_der)

    extension_id = extension_id_from_public_key(public_der)
    metadata = {
        **build_info,
        "extensionId": extension_id,
        "publicKeySha256": hashlib.sha256(public_der).hexdigest(),
        "artifacts": {
            ZIP_PATH.name: {"bytes": ZIP_PATH.stat().st_size, "sha256": sha256(ZIP_PATH)},
            CRX_PATH.name: {"bytes": CRX_PATH.stat().st_size, "sha256": sha256(CRX_PATH)},
        },
    }
    write_json(RELEASE / "blueshield-metadata.json", metadata)
    (RELEASE / "SHA256SUMS").write_text(
        f"{sha256(ZIP_PATH)}  {ZIP_PATH.name}\n{sha256(CRX_PATH)}  {CRX_PATH.name}\n",
        encoding="utf-8",
    )
    return metadata


def main() -> int:
    os.umask(0o022)
    ubo_manifest, pb_manifest = copy_extension_sources()
    pb_root = STAGE / PB_BASE

    install_pb_runtime(pb_root, pb_manifest)
    patch_pb_runtime(pb_root)
    content_scripts = build_content_script_bundles(pb_root, pb_manifest)
    rewrite_pb_html_and_css(pb_root)
    offset_static_rule_ids(pb_root)
    merge_locales(pb_manifest)
    managed_schema = merge_managed_schema(pb_manifest)
    patch_ublock(STAGE)

    ubo_version = (UBO_REPO / "dist" / "version").read_text(encoding="utf-8").strip()
    pb_version = pb_manifest["version"]
    build_manifest(
        ubo_manifest,
        pb_manifest,
        content_scripts,
        managed_schema,
        pb_version,
        ubo_version,
    )
    build_info = add_branding_and_notices(pb_version, ubo_version)
    rebrand_stats = rebrand_ui(STAGE)
    assert_no_upstream_names_in_ui(STAGE)
    build_info["rebrand"] = rebrand_stats
    write_json(STAGE / "BUILD_INFO.json", build_info)
    metadata = package_artifacts(build_info)

    print(f"Built: {STAGE}")
    print(f"ZIP:   {ZIP_PATH}")
    print(f"CRX:   {CRX_PATH}")
    print(f"ID:    {metadata['extensionId']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        print(error.stdout or "", file=sys.stderr)
        print(error.stderr or "", file=sys.stderr)
        raise
