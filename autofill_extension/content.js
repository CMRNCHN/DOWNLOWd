// Provision Field Autofill — content script.
//
// Reads the profile handoff.js stored in chrome.storage.local, scans the
// page for form fields, and offers a "Fill" affordance next to each match.
// Never submits a form and never sends data anywhere — this only sets
// local input values on the page already open in front of the operator.

(function () {
  const PENDING_KEY = "provisionPendingAutofill";
  const MAX_AGE_MS = 20 * 60 * 1000; // matches this app's "short-lived TEMP" philosophy
  const FILLED_ATTR = "data-provision-filled";
  const MARK_ATTR = "data-provision-matched";

  let profile = null;
  let debounceTimer = null;

  function fieldLabelText(input) {
    if (input.id) {
      const byFor = document.querySelector(`label[for="${CSS.escape(input.id)}"]`);
      if (byFor) return byFor.textContent.trim();
    }
    const parentLabel = input.closest("label");
    if (parentLabel) return parentLabel.textContent.trim();
    return "";
  }

  function guessKey(input) {
    const name = (input.name || "").toLowerCase();
    const id = (input.id || "").toLowerCase();
    const autocomplete = (input.autocomplete || "").toLowerCase();
    const label = fieldLabelText(input).toLowerCase();
    const type = (input.type || "").toLowerCase();
    const haystack = `${name} ${id} ${autocomplete} ${label}`;

    // 1. Exact match against this service's known site field names, e.g.
    //    Hyatt's "confirmPassword" — most reliable, straight from
    //    SITE_AUTOFILL_SPECS on the Python side.
    if (profile.site_field_names) {
      for (const [siteFieldName, dataKey] of Object.entries(profile.site_field_names)) {
        const needle = siteFieldName.toLowerCase();
        if (name === needle || id === needle) return dataKey;
      }
    }

    // 2. Heuristics, in order of confidence.
    if (type === "password") {
      return /confirm|repeat|verify/.test(haystack) ? "confirm_password" : "password";
    }
    if (type === "email" || /email/.test(haystack)) return "email";
    if (/first.?name|given.?name/.test(haystack)) return "first_name";
    if (/last.?name|family.?name|surname/.test(haystack)) return "last_name";
    if (/zip|postal/.test(haystack)) return "postal";
    if (autocomplete === "username" && !haystack.includes("email")) return "username";
    return null;
  }

  function setNativeValue(input, value) {
    const proto = input.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value") && Object.getOwnPropertyDescriptor(proto, "value").set;
    if (setter) {
      setter.call(input, value);
    } else {
      input.value = value;
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function flashFilled(input) {
    const prevOutline = input.style.outline;
    input.style.outline = "2px solid #16a34a";
    setTimeout(() => {
      input.style.outline = prevOutline;
    }, 900);
  }

  function fillOne(input, key) {
    const value = profile.fields[key];
    if (!value) return false;
    setNativeValue(input, value);
    input.setAttribute(FILLED_ATTR, "1");
    flashFilled(input);
    return true;
  }

  function makeBadge(input, key) {
    const rect = input.getBoundingClientRect();
    const badge = document.createElement("button");
    badge.type = "button";
    badge.textContent = "Fill";
    badge.setAttribute("data-provision-badge", "1");
    badge.style.cssText = [
      "position:absolute",
      `top:${window.scrollY + rect.top + rect.height / 2 - 11}px`,
      `left:${window.scrollX + rect.right + 6}px`,
      "z-index:2147483647",
      "font:600 11px -apple-system,sans-serif",
      "padding:3px 8px",
      "border-radius:999px",
      "border:1px solid #16a34a",
      "background:#f0fdf4",
      "color:#15803d",
      "cursor:pointer",
      "box-shadow:0 1px 2px rgba(0,0,0,.15)",
    ].join(";");
    badge.title = `Fill from Provision profile (${key.replace(/_/g, " ")})`;
    badge.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      fillOne(input, key);
    });
    document.body.appendChild(badge);
    return badge;
  }

  function makeFillAllButton() {
    if (document.querySelector('[data-provision-fill-all="1"]')) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Provision: Fill all matched fields";
    btn.setAttribute("data-provision-fill-all", "1");
    btn.style.cssText = [
      "position:fixed",
      "bottom:20px",
      "right:20px",
      "z-index:2147483647",
      "font:600 13px -apple-system,sans-serif",
      "padding:10px 16px",
      "border-radius:999px",
      "border:none",
      "background:#16a34a",
      "color:#fff",
      "cursor:pointer",
      "box-shadow:0 2px 8px rgba(0,0,0,.25)",
    ].join(";");
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      let count = 0;
      document.querySelectorAll(`[${MARK_ATTR}]`).forEach((el) => {
        const key = el.getAttribute(MARK_ATTR);
        if (fillOne(el, key)) count += 1;
      });
      btn.textContent = count > 0 ? `Filled ${count} field(s)` : "Nothing to fill";
      setTimeout(() => {
        btn.textContent = "Provision: Fill all matched fields";
      }, 2000);
    });
    document.body.appendChild(btn);
  }

  function clearBadges() {
    document.querySelectorAll('[data-provision-badge="1"]').forEach((el) => el.remove());
  }

  function scan() {
    if (!profile) return;
    clearBadges();
    const inputs = document.querySelectorAll("input, textarea");
    let matched = 0;
    inputs.forEach((input) => {
      if (input.disabled || input.type === "hidden" || input.type === "submit" || input.type === "button") return;
      if (input.getAttribute(FILLED_ATTR)) return; // already filled once, don't re-badge
      const key = guessKey(input);
      if (!key || !profile.fields[key]) {
        input.removeAttribute(MARK_ATTR);
        return;
      }
      input.setAttribute(MARK_ATTR, key);
      makeBadge(input, key);
      matched += 1;
    });
    if (matched > 0) makeFillAllButton();
  }

  function scheduleScan() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(scan, 400);
  }

  function init() {
    chrome.storage.local.get(PENDING_KEY, (result) => {
      const pending = result && result[PENDING_KEY];
      if (!pending || !pending.fields) return;
      if (Date.now() - (pending.createdAt || 0) > MAX_AGE_MS) return; // stale, ignore
      profile = pending;
      scan();
      const observer = new MutationObserver(scheduleScan);
      observer.observe(document.body, { childList: true, subtree: true });
      window.addEventListener("resize", scheduleScan);
    });
  }

  if (document.body) {
    init();
  } else {
    document.addEventListener("DOMContentLoaded", init);
  }
})();
