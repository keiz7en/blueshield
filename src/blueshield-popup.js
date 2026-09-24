"use strict";

const PRIVACY_COMPONENT = "__PRIVACY_COMPONENT__";

const LEVELS = { 0: "Off", 1: "Basic", 2: "Balanced", 3: "Strict" };

const askBlocking = (message) => chrome.runtime.sendMessage(message);
const askPrivacy = (message) =>
  chrome.runtime.sendMessage({ ...message, __blueshieldComponent: PRIVACY_COMPONENT });

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

for (const button of document.querySelectorAll("[data-open]")) {
  button.addEventListener("click", async () => {
    const hash = button.dataset.open.includes("#")
      ? `#${button.dataset.open.split("#")[1]}`
      : "";
    const page = button.dataset.open.split("#")[0];
    await chrome.tabs.create({ url: chrome.runtime.getURL(page) + hash });
    window.close();
  });
}

async function main() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const url = (tab && tab.url) || "";
  const host = hostOf(url);
  const toggle = document.querySelector("#toggle-site");
  const siteName = document.querySelector("#site-name");
  const blockingStat = document.querySelector("#stat-blocking");
  const trackerStat = document.querySelector("#stat-trackers");

  if (!host || !/^https?:/i.test(url)) {
    siteName.textContent = "No website on this tab";
    blockingStat.textContent = "–";
    trackerStat.textContent = "–";
    toggle.disabled = true;
    return;
  }

  siteName.textContent = host;

  const level = await askBlocking({ what: "getFilteringMode", hostname: host })
    .catch(() => null);
  blockingStat.textContent = LEVELS[level] || "–";

  const tabData = await askPrivacy({ type: "getPopupData", tabId: tab.id, tabUrl: url })
    .catch(() => null);
  if (tabData && !tabData.noTabData) {
    const count = tabData.trackerCount || Object.keys(tabData.trackers || {}).length;
    trackerStat.textContent = String(count);
    const paused = tabData.enabled === false;
    toggle.textContent = paused ? "Resume on this site" : "Pause on this site";
    toggle.onclick = async () => {
      toggle.disabled = true;
      try {
        if (paused) {
          await askPrivacy({ type: "reenableOnSiteFromPopup", tabHost: host, tabId: tab.id });
          if (level !== 0) {
            await askBlocking({ what: "setFilteringMode", hostname: host, level: 1 });
          }
        } else {
          await askPrivacy({ type: "disableOnSiteFromPopup", tabHost: host, tabId: tab.id });
          await askBlocking({ what: "setFilteringMode", hostname: host, level: 0 });
        }
        window.close();
      } catch {
        toggle.disabled = false;
      }
    };
  } else {
    trackerStat.textContent = "–";
    toggle.disabled = true;
  }
}

main().catch(() => {
  document.querySelector("#site-name").textContent = "BlueShield";
});
