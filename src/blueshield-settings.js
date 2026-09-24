"use strict";

/**
 * BlueShield unified settings.
 *
 * Talks to both bundled engines through the extension service worker:
 *   - blocking engine  : messages with a `what` field
 *   - tracker protection: messages with a `type` field, tagged with the
 *                         component id injected at build time
 */

const PRIVACY_COMPONENT = "__PRIVACY_COMPONENT__";
const PRIVACY_STORAGE_PREFIX = "__PRIVACY_STORAGE_PREFIX__";
const VERSION = "__BLUESHIELD_VERSION__";
const BLOCKING_ENGINE_VERSION = "__BLOCKING_ENGINE_VERSION__";
const PRIVACY_ENGINE_VERSION = "__PRIVACY_ENGINE_VERSION__";

const PRIVACY_STORES = [
  "snitch_map",
  "action_map",
  "cookieblock_list",
  "dnt_hashes",
  "settings_map",
  "private_storage",
  "tracking_map",
  "fp_scripts",
];

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

/* ------------------------------------------------------------------ *
 * messaging helpers
 * ------------------------------------------------------------------ */

async function askBlocking(message) {
  return chrome.runtime.sendMessage(message);
}

async function askPrivacy(message) {
  return chrome.runtime.sendMessage({ ...message, __blueshieldComponent: PRIVACY_COMPONENT });
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab || null;
}

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/* ------------------------------------------------------------------ *
 * tiny UI helpers
 * ------------------------------------------------------------------ */

let toastTimer;

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", Boolean(isError));
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 6000 : 3200);
}

function showError(error) {
  const node = $("#status");
  if (!error) {
    node.hidden = true;
    return;
  }
  node.textContent = String(error.message || error);
  node.hidden = false;
}

async function guard(button, task, successMessage) {
  if (button) button.disabled = true;
  showError(null);
  try {
    await task();
    if (successMessage) toast(successMessage);
  } catch (error) {
    toast(error && error.message ? error.message : "Something went wrong", true);
  } finally {
    if (button) button.disabled = false;
  }
}

function formatBytes(bytes) {
  if (typeof bytes !== "number" || Number.isNaN(bytes)) return "–";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function levelName(level) {
  return { 0: "Off", 1: "Basic", 2: "Balanced", 3: "Strict" }[level] || "Unknown";
}

/* ------------------------------------------------------------------ *
 * navigation
 * ------------------------------------------------------------------ */

const loadedTabs = new Set();

function selectTab(name) {
  $$(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  $$(".panel").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.panel === name);
  });
  if (typeof location.hash === "string" && location.hash.slice(1) !== name) {
    history.replaceState(null, "", `#${name}`);
  }
  const loader = tabLoaders[name];
  if (loader && !loadedTabs.has(name)) {
    loadedTabs.add(name);
    Promise.resolve()
      .then(loader)
      .catch((error) => {
        loadedTabs.delete(name);
        toast(error && error.message ? error.message : "Could not load section", true);
      });
  }
}

/* ------------------------------------------------------------------ *
 * overview
 * ------------------------------------------------------------------ */

let currentSite = { host: "", paused: false };

async function loadOverview() {
  const [blocking, privacy, bytes] = await Promise.all([
    askBlocking({ what: "getOptionsPageData" }).catch(() => null),
    askPrivacy({ type: "getOptionsData" }).catch(() => null),
    chrome.storage.local.getBytesInUse(null).catch(() => undefined),
  ]);

  $("#ov-blocking-level").textContent = blocking
    ? levelName(blocking.defaultFilteringMode)
    : "–";
  $("#ov-list-count").textContent = blocking
    ? String((blocking.enabledRulesets || []).length)
    : "–";
  $("#ov-tracker-count").textContent = privacy
    ? String(Object.keys(privacy.trackers || {}).length)
    : "–";
  $("#ov-storage").textContent = formatBytes(bytes);

  const tab = await activeTab();
  const host = tab ? hostOf(tab.url || "") : "";
  currentSite = { host, paused: false };
  const row = $("#ov-site-row");
  const hint = $("#ov-site-hint");
  const button = $("#ov-toggle-site");
  if (!host || /^(chrome|edge|about|chrome-extension|devtools|moz-extension):/i.test(tab.url || "")) {
    $("#ov-site").textContent = "Not available";
    button.disabled = true;
    hint.textContent = "Open a normal web page to control protection per site.";
    return;
  }
  button.disabled = false;
  $("#ov-site").textContent = host;
  const settings = (privacy && privacy.settings) || {};
  currentSite.paused = (settings.disabledSites || []).includes(host);
  button.textContent = currentSite.paused ? "Resume on this site" : "Pause on this site";
  hint.textContent = currentSite.paused
    ? "BlueShield is paused here: blocking and tracker protection are off."
    : "Blocking and tracker protection are active here.";
  row.dataset.host = host;
}

$("#ov-toggle-site").addEventListener("click", (event) => {
  const button = event.currentTarget;
  const host = $("#ov-site-row").dataset.host;
  if (!host) return;
  guard(button, async () => {
    if (currentSite.paused) {
      await askPrivacy({ type: "reenableOnSites", domains: [host] });
      const level = await askBlocking({ what: "getFilteringMode", hostname: host });
      if (level !== 0) {
        await askBlocking({ what: "setFilteringMode", hostname: host, level: 1 });
      }
    } else {
      await askPrivacy({ type: "disableOnSite", domain: host });
      await askBlocking({ what: "setFilteringMode", hostname: host, level: 0 });
    }
    await loadOverview();
    await Promise.all([loadTrackerToggles(), loadAllowList()]);
    toast(currentSite.paused ? "Protection resumed here" : "Protection paused here");
  });
});

/* ------------------------------------------------------------------ *
 * protection
 * ------------------------------------------------------------------ */

let optionsData = null;

const BLOCKING_TOGGLES = [
  {
    key: "autoReload",
    title: "Reload pages automatically",
    hint: "Reload tabs once after filter lists update.",
    apply: (state) => askBlocking({ what: "setAutoReload", state }),
  },
  {
    key: "showBlockedCount",
    title: "Show blocked count on the toolbar icon",
    hint: "Adds the number of blocked requests to the extension badge.",
    apply: (state) => askBlocking({ what: "setShowBlockedCount", state }),
  },
  {
    key: "popupBlockMode",
    title: "Block pop-ups",
    hint: "Stops websites from opening new windows or tabs on their own.",
    apply: (state) => askBlocking({ what: "setPopupBlockMode", state }),
  },
  {
    key: "strictBlockMode",
    title: "Strict blocking",
    hint: "Blocks requests before they leave the browser, on every site.",
    needsOmnipotence: true,
    apply: (state) => askBlocking({ what: "setStrictBlockMode", state }),
  },
  {
    key: "developerMode",
    title: "Advanced mode",
    hint: "Shows the rule editor and troubleshooting tools.",
    apply: (state) => askBlocking({ what: "setDeveloperMode", state }),
  },
];

const PRIVACY_TOGGLES = [
  { key: "showCounter", title: "Show blocked tracker count", hint: "Adds the number of blocked trackers to the badge." },
  { key: "sendDNTSignal", title: "Send Do Not Track", hint: "Adds the Do Not Track header to requests." },
  { key: "checkForDNTPolicy", title: "Respect Do Not Track sites", hint: "Stops blocking on sites that send a DNT header.", indent: true },
  { key: "learnLocally", title: "Learn from browsing", hint: "Detects tracking while you browse and blocks it automatically." },
  { key: "showNonTrackingDomains", title: "Show harmless domains", hint: "Lists domains that only appear to track but do not.", indent: true },
  { key: "learnInIncognito", title: "Learn in private windows", hint: "Keeps learning when browsing privately.", indent: true },
  { key: "disableNetworkPrediction", title: "Disable network prediction", hint: "Stops the browser from preloading pages you are likely to open." },
  { key: "disableHyperlinkAuditing", title: "Disable hyperlink auditing", hint: "Stops pages from tracking which links you follow." },
  { key: "disableGoogleNavErrorService", title: "Disable navigation error pings", hint: "Stops the browser from sending failed navigation data." },
  { key: "disableTopics", title: "Disable topics tracking", hint: "Stops the browser from inferring your interests from browsing." },
];

function renderToggles(container, definitions, source, onToggle) {
  container.textContent = "";
  for (const definition of definitions) {
    const label = document.createElement("label");
    label.className = "toggle";
    if (definition.indent) label.style.marginInlineStart = "22px";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = Boolean(source[definition.key]);
    if (definition.disabled) {
      input.disabled = true;
      label.dataset.disabled = "true";
    }
    const body = document.createElement("span");
    body.className = "toggle-body";
    const strong = document.createElement("strong");
    strong.textContent = definition.title;
    const small = document.createElement("small");
    small.textContent = definition.hint;
    body.append(strong, small);
    input.addEventListener("change", () => {
      guard(null, () => onToggle(definition, input.checked), "Saved");
    });
    label.append(input, body);
    container.append(label);
  }
}

async function loadProtection() {
  optionsData = (await askBlocking({ what: "getOptionsPageData" })) || {};
  const privacy = (await askPrivacy({ type: "getOptionsData" })) || {};
  const privacySettings = privacy.settings || {};

  const radios = $$("#filter-modes input[type=radio]");
  const level = optionsData.defaultFilteringMode || 0;
  radios.forEach((radio) => {
    radio.checked = Number(radio.value) === level;
    radio.disabled = false;
  });
  radios.forEach((radio) => {
    radio.addEventListener("change", async () => {
      if (!radio.checked) return;
      const wanted = Number(radio.value);
      await guard(null, async () => {
        if (wanted > 1) {
          const granted = await chrome.permissions.request({ origins: ["<all_urls>"] });
          if (!granted) {
            toast("Extra site access is required for that level", true);
            radios.forEach((item) => { item.checked = Number(item.value) === level; });
            return;
          }
        }
        const actual = await askBlocking({ what: "setDefaultFilteringMode", level: wanted });
        if (actual !== wanted) {
          toast(`Level limited to ${levelName(actual)}`, true);
        }
        await loadProtection();
      }, "Default level updated");
    });
  });

  renderToggles(
    $("#behaviour-toggles"),
    BLOCKING_TOGGLES.map((definition) => ({
      ...definition,
      disabled: definition.needsOmnipotence && optionsData.hasOmnipotence === false,
    })),
    optionsData,
    (definition, state) => definition.apply(state),
  );

  await loadTrackerToggles(privacySettings);
}

async function loadTrackerToggles(preset) {
  const privacy = preset ? { settings: preset } : (await askPrivacy({ type: "getOptionsData" })) || {};
  renderToggles($("#tracker-toggles"), PRIVACY_TOGGLES, privacy.settings || {}, async (definition, state) => {
    await askPrivacy({ type: "updateSettings", data: { [definition.key]: state } });
    if (definition.key === "sendDNTSignal" || definition.key.startsWith("disable")) {
      await askPrivacy({ type: "setPrivacyOverrides" });
    }
  });
}

/* ------------------------------------------------------------------ *
 * filter lists
 * ------------------------------------------------------------------ */

let listState = { details: [], enabled: new Set() };

function groupLabel(group) {
  return {
    default: "Recommended",
    malware: "Security",
    annoyances: "Annoyances",
    experimental: "Experimental",
  }[group] || group || "Other";
}

async function loadLists() {
  const data = (await askBlocking({ what: "getOptionsPageData" })) || {};
  listState.details = data.rulesetDetails || [];
  listState.enabled = new Set(data.enabledRulesets || []);
  const max = data.maxNumberOfEnabledRulesets || 0;

  const container = $("#lists-container");
  container.textContent = "";
  const groups = new Map();
  for (const detail of listState.details) {
    const key = detail.group || "default";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(detail);
  }

  for (const [group, entries] of groups) {
    const section = document.createElement("div");
    section.className = "list-group";
    const heading = document.createElement("h3");
    heading.textContent = groupLabel(group);
    section.append(heading);

    for (const entry of entries) {
      const row = document.createElement("div");
      row.className = "list-row";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = listState.enabled.has(entry.id);
      input.addEventListener("change", () => {
        guard(null, async () => {
          if (input.checked) listState.enabled.add(entry.id);
          else listState.enabled.delete(entry.id);
          await askBlocking({
            what: "applyRulesets",
            enabledRulesets: Array.from(listState.enabled),
          });
          await loadOverview();
        }, "Filter lists updated");
      });
      const name = document.createElement("span");
      name.className = "grow";
      name.textContent = entry.name || entry.id;
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = (entry.filters && entry.filters.total
        ? `${entry.filters.total.toLocaleString()} filters`
        : entry.id);
      row.append(input, name, tag);
      section.append(row);
    }
    container.append(section);
  }

  $("#lists-summary").textContent =
    `${listState.enabled.size} enabled${max ? ` of at most ${max}` : ""} · ${listState.details.length} available`;
  await loadImportedLists();
}

async function loadImportedLists() {
  const container = $("#imported-lists");
  container.textContent = "";
  let imported = [];
  try {
    imported = (await askBlocking({ what: "getImportedLists" })) || [];
  } catch {
    imported = [];
  }
  if (imported.length === 0) {
    const note = document.createElement("span");
    note.className = "muted small";
    note.textContent = "No custom lists added yet.";
    container.append(note);
    return;
  }
  for (const list of imported) {
    const chip = document.createElement("span");
    chip.className = "chip";
    const label = document.createElement("span");
    label.textContent = list.name || list.url || list.id;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "Remove list";
    remove.addEventListener("click", () => {
      guard(remove, async () => {
        const enabled = Array.from(listState.enabled);
        await askBlocking({
          what: "applyRulesets",
          enabledRulesets: enabled.filter((id) => id !== list.id),
          toRemove: [list.id],
        });
        listState.enabled.delete(list.id);
        await loadLists();
      }, "List removed");
    });
    chip.append(label, remove);
    container.append(chip);
  }
}

$("#lists-refresh").addEventListener("click", (event) => {
  loadedTabs.delete("lists");
  guard(event.currentTarget, loadLists, "Filter lists reloaded");
});

$("#list-add").addEventListener("click", (event) => {
  const input = $("#list-url");
  const url = input.value.trim();
  if (!url) return;
  guard(event.currentTarget, async () => {
    await askBlocking({ what: "importFilterList", url });
    input.value = "";
    await loadLists();
    await loadOverview();
  }, "List added");
});

/* ------------------------------------------------------------------ *
 * custom rules
 * ------------------------------------------------------------------ */

async function loadCustomRules() {
  const [filters, sandbox] = await Promise.all([
    askBlocking({ what: "getAllCustomFilters" }).catch(() => []),
    askBlocking({ what: "getSandboxFilters" }).catch(() => ""),
  ]);
  $("#sandbox").value = typeof sandbox === "string" ? sandbox : "";

  const list = $("#cf-list");
  list.textContent = "";
  const entries = Array.isArray(filters) ? filters : [];
  if (entries.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No per-site rules yet.";
    list.append(empty);
    return;
  }
  for (const [hostname, selectors] of entries) {
    const row = document.createElement("div");
    row.className = "rule";
    const host = document.createElement("code");
    host.textContent = hostname;
    const value = document.createElement("span");
    value.className = "grow muted";
    value.textContent = (selectors || []).join(", ");
    const removeAll = document.createElement("button");
    removeAll.className = "btn";
    removeAll.textContent = "Remove all";
    removeAll.addEventListener("click", () => {
      guard(removeAll, async () => {
        await askBlocking({ what: "removeAllCustomFilters", hostname });
        await loadCustomRules();
      }, "Rules removed");
    });
    row.append(host, value, removeAll);
    list.append(row);
  }
}

$("#cf-add").addEventListener("click", (event) => {
  const hostInput = $("#cf-host");
  const selectorInput = $("#cf-selectors");
  const hostname = hostInput.value.trim();
  const selectors = selectorInput.value.split(",").map((item) => item.trim()).filter(Boolean);
  if (!hostname || selectors.length === 0) {
    toast("Enter a website and at least one selector", true);
    return;
  }
  guard(event.currentTarget, async () => {
    await askBlocking({ what: "addCustomFilters", hostname, selectors });
    hostInput.value = "";
    selectorInput.value = "";
    await loadCustomRules();
  }, "Rule added");
});

$("#sandbox-save").addEventListener("click", (event) => {
  const text = $("#sandbox").value;
  guard(event.currentTarget, async () => {
    await askBlocking({ what: "setSandboxFilters", text });
    await loadCustomRules();
  }, "Advanced rules saved");
});

/* ------------------------------------------------------------------ *
 * trackers
 * ------------------------------------------------------------------ */

let trackerData = { trackers: {}, cookieblocked: {} };

const TRACKER_STATES = {
  block: { label: "Blocked", className: "state-block" },
  cookieblock: { label: "Cookie blocked", className: "state-cookieblock" },
  dnt: { label: "Do Not Track", className: "state-dnt" },
  noaction: { label: "Allowed", className: "state-noaction" },
  notracking: { label: "Harmless", className: "state-noaction" },
};

function trackerState(domain) {
  const action = trackerData.trackers[domain] || "noaction";
  if (action === "block") return "block";
  if (action === "cookieblock") return "cookieblock";
  if (action === "dnt") return "dnt";
  if (action === "notracking") return "notracking";
  return "noaction";
}

async function loadTrackers() {
  const privacy = (await askPrivacy({ type: "getOptionsData" })) || {};
  trackerData = { trackers: privacy.trackers || {}, cookieblocked: privacy.cookieblocked || {} };
  await loadAllowList();
  renderTrackers();
}

function renderTrackers() {
  const list = $("#tracker-list");
  const search = $("#tracker-search").value.trim().toLowerCase();
  const filter = $("#tracker-filter").value;
  list.textContent = "";

  const domains = Object.keys(trackerData.trackers).sort();
  const shown = domains.filter((domain) => {
    if (search && !domain.toLowerCase().includes(search)) return false;
    if (filter) {
      const state = trackerState(domain);
      if (filter === "noaction" && !["noaction", "notracking"].includes(state)) return false;
      if (filter !== "noaction" && state !== filter) return false;
    }
    return true;
  });

  if (shown.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = domains.length === 0
      ? "Nothing learned yet — browse a little and come back."
      : "No domains match your search.";
    list.append(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const domain of shown.slice(0, 400)) {
    const state = trackerState(domain);
    const meta = TRACKER_STATES[state] || TRACKER_STATES.noaction;
    const row = document.createElement("div");
    row.className = "tracker";

    const name = document.createElement("span");
    name.className = "domain";
    name.textContent = domain;

    const badge = document.createElement("span");
    badge.className = `state ${meta.className}`;
    badge.textContent = meta.label;

    const actions = document.createElement("span");
    for (const [value, title] of [["block", "Block"], ["cookieblock", "Block cookies"], ["noaction", "Allow"]]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = title;
      button.addEventListener("click", () => {
        guard(button, async () => {
          const response = await askPrivacy({
            type: "saveOptionsToggle",
            domain,
            action: value,
          });
          if (response && response.trackers) {
            trackerData.trackers = response.trackers;
          }
          renderTrackers();
        }, "Saved");
      });
      actions.append(button);
    }

    row.append(name, badge, actions);
    fragment.append(row);
  }
  list.append(fragment);
  if (shown.length > 400) {
    const note = document.createElement("div");
    note.className = "empty";
    note.textContent = `Showing the first 400 of ${shown.length} matches — use search to narrow it down.`;
    list.append(note);
  }
}

$("#tracker-search").addEventListener("input", renderTrackers);
$("#tracker-filter").addEventListener("change", renderTrackers);

async function loadAllowList() {
  const privacy = (await askPrivacy({ type: "getOptionsData" })) || {};
  const disabled = (privacy.settings && privacy.settings.disabledSites) || [];
  const container = $("#allow-list");
  container.textContent = "";
  if (disabled.length === 0) {
    const note = document.createElement("span");
    note.className = "muted small";
    note.textContent = "Protection is active on every site.";
    container.append(note);
    return;
  }
  for (const domain of disabled) {
    const chip = document.createElement("span");
    chip.className = "chip";
    const label = document.createElement("span");
    label.textContent = domain;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "Resume protection";
    remove.addEventListener("click", () => {
      guard(remove, async () => {
        await askPrivacy({ type: "reenableOnSites", domains: [domain] });
        await loadAllowList();
        await loadOverview();
      }, "Protection resumed");
    });
    chip.append(label, remove);
    container.append(chip);
  }
}

$("#allow-add").addEventListener("click", (event) => {
  const input = $("#allow-host");
  const domain = input.value.trim().toLowerCase();
  if (!domain) return;
  guard(event.currentTarget, async () => {
    await askPrivacy({ type: "disableOnSite", domain });
    input.value = "";
    await loadAllowList();
    await loadOverview();
  }, "Protection paused for that site");
});

/* ------------------------------------------------------------------ *
 * data & storage
 * ------------------------------------------------------------------ */

async function storageSnapshot() {
  const [bytes, local] = await Promise.all([
    chrome.storage.local.getBytesInUse(null),
    chrome.storage.local.get(null),
  ]);
  return { bytes, keys: Object.keys(local) };
}

async function loadStorage() {
  const { bytes, keys } = await storageSnapshot();
  $("#st-bytes").textContent = formatBytes(bytes);
  $("#st-keys").textContent = `${keys.length} stored entries`;
  $("#st-settings").textContent = String(keys.filter((key) => !key.startsWith(`${PRIVACY_STORAGE_PREFIX}tracking_map`)).length);
}

$("#st-save").addEventListener("click", (event) => {
  guard(event.currentTarget, async () => {
    // Ask the tracker engine to flush every store to extension storage now.
    await askPrivacy({ type: "syncStorage" });
    const { bytes, keys } = await storageSnapshot();
    $("#st-bytes").textContent = formatBytes(bytes);
    $("#st-keys").textContent = `${keys.length} stored entries`;
  }, "All settings saved to storage");
});

async function buildBackup() {
  const [config, sandbox, customFilters, importedLists, privacy] = await Promise.all([
    askBlocking({ what: "getCurrentConfig" }).catch(() => ({})),
    askBlocking({ what: "getSandboxFilters" }).catch(() => ""),
    askBlocking({ what: "getAllCustomFilters" }).catch(() => []),
    askBlocking({ what: "getImportedLists" }).catch(() => []),
    askPrivacy({ type: "getOptionsData" }).catch(() => ({})),
  ]);
  const maps = await chrome.storage.local.get(
    PRIVACY_STORES.map((store) => PRIVACY_STORAGE_PREFIX + store),
  );
  const privacyMaps = {};
  for (const store of PRIVACY_STORES) {
    const key = PRIVACY_STORAGE_PREFIX + store;
    if (Object.prototype.hasOwnProperty.call(maps, key)) {
      privacyMaps[store] = maps[key];
    }
  }
  return {
    format: "blueshield-backup",
    version: VERSION,
    exportedAt: new Date().toISOString(),
    blocking: { config, sandbox, customFilters, importedLists },
    trackers: { settings: (privacy && privacy.settings) || {}, maps: privacyMaps },
  };
}

$("#st-export").addEventListener("click", (event) => {
  guard(event.currentTarget, async () => {
    const backup = await buildBackup();
    const blob = new Blob([JSON.stringify(backup, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `blueshield-backup-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }, "Backup downloaded");
});

$("#st-import").addEventListener("click", () => $("#st-file").click());

$("#st-file").addEventListener("change", (event) => {
  const file = event.target.files && event.target.files[0];
  if (!file) return;
  guard(null, async () => {
    const backup = JSON.parse(await file.text());
    if (backup.format !== "blueshield-backup") {
      throw new Error("That file is not a BlueShield backup");
    }
    const blocking = backup.blocking || {};
    const config = blocking.config || {};
    for (const key of ["autoReload", "showBlockedCount", "popupBlockMode", "strictBlockMode", "developerMode"]) {
      if (typeof config[key] === "boolean") {
        const what = {
          autoReload: "setAutoReload",
          showBlockedCount: "setShowBlockedCount",
          popupBlockMode: "setPopupBlockMode",
          strictBlockMode: "setStrictBlockMode",
          developerMode: "setDeveloperMode",
        }[key];
        await askBlocking({ what, state: config[key] });
      }
    }
    if (typeof config.defaultFilteringMode === "number") {
      await askBlocking({ what: "setDefaultFilteringMode", level: config.defaultFilteringMode });
    }
    if (Array.isArray(config.enabledRulesets)) {
      await askBlocking({ what: "applyRulesets", enabledRulesets: config.enabledRulesets });
    }
    if (typeof blocking.sandbox === "string") {
      await askBlocking({ what: "setSandboxFilters", text: blocking.sandbox });
    }
    for (const [hostname, selectors] of blocking.customFilters || []) {
      if (Array.isArray(selectors) && selectors.length !== 0) {
        await askBlocking({ what: "addCustomFilters", hostname, selectors });
      }
    }
    for (const list of blocking.importedLists || []) {
      if (list && list.url && list.enabled !== false) {
        await askBlocking({ what: "importFilterList", url: list.url }).catch(() => {});
      }
    }
    const trackers = backup.trackers || {};
    if (trackers.settings && Object.keys(trackers.settings).length !== 0) {
      const allowed = new Set([
        "checkForDNTPolicy", "disabledSites", "disableGoogleNavErrorService",
        "disableHyperlinkAuditing", "disableNetworkPrediction", "disableTopics",
        "learnInIncognito", "learnLocally", "sendDNTSignal", "showCounter",
        "showDisabledSitesTip", "showExpandedTrackingSection", "showNonTrackingDomains",
        "widgetReplacementExceptions", "widgetSiteAllowlist",
      ]);
      const data = {};
      for (const [key, value] of Object.entries(trackers.settings)) {
        if (allowed.has(key)) data[key] = value;
      }
      if (Object.keys(data).length !== 0) {
        await askPrivacy({ type: "updateSettings", data });
        await askPrivacy({ type: "setPrivacyOverrides" });
      }
    }
    if (trackers.maps && Object.keys(trackers.maps).length !== 0) {
      await askPrivacy({ type: "mergeUserData", data: trackers.maps });
    }
    await askPrivacy({ type: "syncStorage" });
    event.target.value = "";
    loadedTabs.clear();
    await Promise.all([loadStorage(), loadOverview()]);
  }, "Backup restored");
});

$("#st-reset-learned").addEventListener("click", (event) => {
  if (!confirm("Reset the learned tracker list? Your settings are kept.")) return;
  guard(event.currentTarget, async () => {
    await askPrivacy({ type: "resetData" });
    await askPrivacy({ type: "syncStorage" });
    loadedTabs.delete("trackers");
    await Promise.all([loadTrackers(), loadOverview()]);
  }, "Learned tracker data reset");
});

$("#st-reset-all").addEventListener("click", (event) => {
  if (!confirm("Erase every setting and all learned tracker data? This cannot be undone.")) return;
  guard(event.currentTarget, async () => {
    await askPrivacy({ type: "removeAllData" });
    await askPrivacy({ type: "syncStorage" });
    loadedTabs.clear();
    await Promise.all([loadStorage(), loadOverview()]);
  }, "All BlueShield data erased");
});

/* ------------------------------------------------------------------ *
 * about
 * ------------------------------------------------------------------ */

function loadAbout() {
  $("#about-version").textContent = VERSION;
  $("#about-blocking").textContent = BLOCKING_ENGINE_VERSION;
  $("#about-privacy").textContent = PRIVACY_ENGINE_VERSION;
  $("#about-id").textContent = chrome.runtime.id;
}

$("#about-copy").addEventListener("click", async (event) => {
  await navigator.clipboard.writeText(chrome.runtime.id);
  const button = event.currentTarget;
  button.textContent = "Copied";
  setTimeout(() => { button.textContent = "Copy"; }, 1200);
});

$("#open-popup").addEventListener("click", () => {
  chrome.action.openPopup().catch(() => {
    chrome.tabs.create({ url: chrome.runtime.getURL("blueshield-settings.html#overview") });
  });
});

/* ------------------------------------------------------------------ *
 * boot
 * ------------------------------------------------------------------ */

const tabLoaders = {
  overview: loadOverview,
  protection: loadProtection,
  lists: loadLists,
  custom: loadCustomRules,
  trackers: loadTrackers,
  storage: loadStorage,
  about: loadAbout,
};

$("#nav").addEventListener("click", (event) => {
  const button = event.target.closest(".nav-item");
  if (!button) return;
  selectTab(button.dataset.tab);
});

const initialTab = location.hash.slice(1);
selectTab(tabLoaders[initialTab] ? initialTab : "overview");
loadAbout();
