// Auto-reload the dashboard when the server-side data version changes.
//
// Why a plain fetch loop instead of a Dash callback:
//   The daily 09:00 pipeline (and the manual "Refresh" button) writes a new
//   data version on the server. A Dash in-place store update only refreshes
//   components wired to that store and silently stops working if the tab was
//   left open while the dashboard process restarted overnight (the 5-minute
//   auto-update task can restart it). A full page reload guarantees every tab
//   always shows the latest data, and this poll is independent of the Dash
//   callback/websocket machinery so it self-heals after a server restart.
(function () {
  "use strict";

  var POLL_MS = 30000; // check every 30s (daily refresh, no need to be aggressive)
  var baseline = null; // data version captured at page load
  var reloading = false;

  function fetchVersion() {
    return fetch("/data-version", { cache: "no-store" })
      .then(function (resp) {
        if (!resp.ok) {
          return null;
        }
        return resp.text();
      })
      .then(function (text) {
        return text == null ? null : text.trim();
      })
      .catch(function () {
        // Network hiccup / server restarting: ignore and try again next tick.
        return null;
      });
  }

  function poll() {
    if (reloading) {
      return;
    }
    fetchVersion().then(function (version) {
      if (version == null || version === "") {
        return; // no version available yet; keep the current baseline
      }
      if (baseline === null) {
        baseline = version; // first successful read becomes the baseline
        return;
      }
      if (version !== baseline) {
        reloading = true;
        window.location.reload();
      }
    });
  }

  // Capture the baseline as soon as possible, then poll on an interval.
  fetchVersion().then(function (version) {
    if (version != null && version !== "") {
      baseline = version;
    }
  });
  setInterval(poll, POLL_MS);
})();
