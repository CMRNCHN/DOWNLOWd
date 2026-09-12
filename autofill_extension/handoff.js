// Reads the profile Provision passed via the URL, stores it for content.js
// to pick up on the actual signup page, then closes itself. Never touches
// any other page or sends this data anywhere else.
(function () {
  const statusEl = document.getElementById("status");

  function setStatus(text, ok) {
    statusEl.textContent = text;
    statusEl.className = ok ? "ok" : "err";
  }

  try {
    const params = new URLSearchParams(location.search);
    const encoded = params.get("data");
    if (!encoded) {
      setStatus("No profile data provided.", false);
      return;
    }
    const json = decodeURIComponent(escape(atob(encoded)));
    const profile = JSON.parse(json);
    if (!profile || typeof profile !== "object" || !profile.service || !profile.fields) {
      setStatus("Malformed profile data.", false);
      return;
    }
    profile.createdAt = Date.now();
    chrome.storage.local.set({ provisionPendingAutofill: profile }, () => {
      if (chrome.runtime.lastError) {
        setStatus("Could not store profile: " + chrome.runtime.lastError.message, false);
        return;
      }
      setStatus(
        `Autofill profile ready for ${profile.service} (${profile.employee_name || "employee"}). Switching to signup…`,
        true
      );
      setTimeout(() => {
        try {
          window.close();
        } catch (e) {
          /* ignore — tab may not be closable, that's fine */
        }
      }, 600);
    });
  } catch (e) {
    setStatus("Error: " + e.message, false);
  }
})();
