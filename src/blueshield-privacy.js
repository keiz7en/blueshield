/* BlueShield page-world privacy layer.
 *
 * Content scripts run in an isolated world, so patches made there are invisible
 * to ordinary page scripts. This file is registered as a MAIN-world content
 * script at document_start and applies the protections that pages could
 * otherwise observe directly:
 *
 * - Canvas readback noise: one color channel of the last pixel returned by
 *   getImageData is flipped by +/-1. Invisible in practice, but it breaks the
 *   exact-match comparisons used by canvas fingerprinting.
 * - Audio readback noise: the first getChannelData read of each AudioBuffer has
 *   sample 0 nudged by 1e-7 (far below audibility), which breaks exact-match
 *   audio fingerprinting.
 * - Privacy Sandbox neutralization: the Topics, Protected Audience, Attribution
 *   Reporting and Private State Tokens entry points are removed so that
 *   `typeof` is not "function" and `in` checks stop seeing them. On browsers
 *   that already removed these APIs the script is a silent no-op.
 *
 * navigator.globalPrivacyControl is deliberately left untouched: it is a
 * privacy opt-out signal and must keep reporting true.
 *
 * Everything is wrapped so a missing prototype, a detached buffer or an exotic
 * page cannot throw or log from this script.
 */
(() => {
  "use strict";
  if (globalThis.__blueshieldPrivacyInstalled) {
    return;
  }
  globalThis.__blueshieldPrivacyInstalled = true;

  const silence = (fn) => {
    try {
      fn();
    } catch (error) {
      // Missing prototypes or frozen intrinsics: skip quietly.
    }
  };

  // Delete a property from whichever object in the prototype chain owns it, so
  // both `typeof obj.name === "function"` and `name in obj` stop seeing it.
  const removeProperty = (root, names) => {
    for (const name of names) {
      let proto = root;
      while (proto) {
        const descriptor = Object.getOwnPropertyDescriptor(proto, name);
        if (descriptor) {
          if (descriptor.configurable) {
            delete proto[name];
          }
          break;
        }
        proto = Object.getPrototypeOf(proto);
      }
    }
  };

  silence(() =>
    removeProperty(Document.prototype, [
      "browsingTopics",
      "hasPrivateToken",
      "mintPrivateToken",
      "redeemPrivateToken",
    ])
  );
  silence(() =>
    removeProperty(Navigator.prototype, [
      "joinAdInterestGroup",
      "runAdAuction",
      "leaveAdInterestGroup",
      "updateAdInterestGroups",
    ])
  );
  silence(() => removeProperty(HTMLAnchorElement.prototype, ["attributionSrc"]));

  silence(() => {
    const proto = CanvasRenderingContext2D.prototype;
    const nativeGetImageData = proto.getImageData;
    if (typeof nativeGetImageData !== "function") {
      return;
    }
    Object.defineProperty(proto, "getImageData", {
      configurable: true,
      writable: true,
      value: function getImageData(sx, sy, sw, sh, settings) {
        const imageData = nativeGetImageData.call(this, sx, sy, sw, sh, settings);
        try {
          const pixels = imageData.data;
          if (pixels.length > 0) {
            // Flip the low bit of the last pixel's first channel: a +/-1 change
            // applied to the returned copy only; the canvas itself is untouched,
            // so repeated reads stay consistent with each other.
            const index = pixels.length - 4;
            pixels[index] = pixels[index] ^ 1;
          }
        } catch (error) {
          // Exotic or detached buffers: return the original data.
        }
        return imageData;
      },
    });
  });

  silence(() => {
    const proto = AudioBuffer.prototype;
    const nativeGetChannelData = proto.getChannelData;
    if (typeof nativeGetChannelData !== "function") {
      return;
    }
    // getChannelData returns the live internal buffer (mutating it is how the
    // noise persists), so each AudioBuffer is nudged exactly once to avoid
    // accumulating drift across repeated reads.
    const nudged = new WeakSet();
    Object.defineProperty(proto, "getChannelData", {
      configurable: true,
      writable: true,
      value: function getChannelData(channel) {
        const data = nativeGetChannelData.call(this, channel);
        try {
          if (!nudged.has(this) && data.length > 0) {
            nudged.add(this);
            data[0] = data[0] + 1e-7;
          }
        } catch (error) {
          // Exotic channel arrays: leave them untouched.
        }
        return data;
      },
    });
  });
})();
