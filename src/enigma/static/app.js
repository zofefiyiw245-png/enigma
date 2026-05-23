/* Enigma — iPhone location spoofer UI */

(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // DOM
  // ---------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);

  const statusDot = $("status-dot");
  const statusText = $("status-text");
  const btnConnect = $("btn-connect");
  const btnDisconnect = $("btn-disconnect");
  const btnSet = $("btn-set");
  const btnReset = $("btn-reset");
  const inputLat = $("coord-lat");
  const inputLon = $("coord-lon");
  const searchForm = $("search-form");
  const searchInput = $("search-input");
  const searchResults = $("search-results");
  const waypointsList = $("waypoints");
  const speedPreset = $("speed-preset");
  const customSpeedWrap = $("custom-speed-wrap");
  const customSpeed = $("custom-speed");
  const loopRoute = $("loop-route");
  const btnRouteStart = $("btn-route-start");
  const btnRouteStop = $("btn-route-stop");
  const btnRouteClear = $("btn-route-clear");
  const routeStatusEl = $("route-status");
  const toastContainer = $("toast-container");

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------
  const state = {
    connected: false,
    waypoints: [],          // [[lat, lon], ...]
    waypointMarkers: [],    // Leaflet markers
    routePolyline: null,
    locationMarker: null,
    cursorMarker: null,     // moves during a route
    routePolling: null,
  };

  // ---------------------------------------------------------------------
  // Map
  // ---------------------------------------------------------------------
  const map = L.map("map", { zoomControl: true }).setView([37.7749, -122.4194], 12);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  map.on("click", (event) => {
    const { lat, lng } = event.latlng;
    if (event.originalEvent && event.originalEvent.shiftKey) {
      addWaypoint(lat, lng);
    } else {
      setCoordInputs(lat, lng);
    }
  });

  // ---------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------
  function toast(message, kind = "info") {
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    toastContainer.appendChild(node);
    setTimeout(() => {
      node.style.opacity = "0";
      node.style.transition = "opacity 0.25s";
      setTimeout(() => node.remove(), 260);
    }, kind === "error" ? 6000 : 3500);
  }

  async function api(path, options = {}) {
    const init = Object.assign({ headers: { "Content-Type": "application/json" } }, options);
    const response = await fetch(path, init);
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      // ignore
    }
    if (!response.ok) {
      const message = (payload && payload.error) || response.statusText || "Request failed";
      const err = new Error(message);
      err.status = response.status;
      throw err;
    }
    return payload;
  }

  function fmtCoord(value) {
    return Number(value).toFixed(6);
  }

  function fmtDistance(m) {
    if (m < 1000) return `${m.toFixed(0)} m`;
    return `${(m / 1000).toFixed(2)} km`;
  }

  function fmtDuration(s) {
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m ${String(sec).padStart(2, "0")}s`;
    if (m > 0) return `${m}m ${String(sec).padStart(2, "0")}s`;
    return `${sec}s`;
  }

  function setCoordInputs(lat, lon) {
    inputLat.value = fmtCoord(lat);
    inputLon.value = fmtCoord(lon);
  }

  function readCoordInputs() {
    const lat = parseFloat(inputLat.value);
    const lon = parseFloat(inputLon.value);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
    return { lat, lon };
  }

  function setDeviceUi(connected, info) {
    state.connected = connected;
    statusDot.classList.toggle("dot-on", connected);
    statusDot.classList.toggle("dot-off", !connected);
    btnConnect.disabled = connected;
    btnDisconnect.disabled = !connected;
    btnSet.disabled = !connected;
    btnReset.disabled = !connected;
    btnRouteStart.disabled = !connected || state.waypoints.length < 2;
    if (connected) {
      const tag = info && info.device && info.device.name ? info.device.name : "iPhone";
      const ios = info && info.device && info.device.ios_version ? `iOS ${info.device.ios_version}` : "";
      const mode = info && info.mode ? `(${info.mode})` : "";
      statusText.textContent = `${tag} ${ios} ${mode}`.trim();
    } else {
      statusText.textContent = "Disconnected";
    }
  }

  // ---------------------------------------------------------------------
  // Waypoints / route polyline
  // ---------------------------------------------------------------------
  function renderWaypoints() {
    waypointsList.innerHTML = "";
    if (state.waypoints.length === 0) {
      waypointsList.classList.add("empty");
      const li = document.createElement("li");
      li.className = "placeholder";
      li.textContent = "Shift-click the map to add waypoints.";
      waypointsList.appendChild(li);
    } else {
      waypointsList.classList.remove("empty");
      state.waypoints.forEach(([lat, lon], idx) => {
        const li = document.createElement("li");
        const code = document.createElement("code");
        code.textContent = `${fmtCoord(lat)}, ${fmtCoord(lon)}`;
        li.appendChild(code);
        const btn = document.createElement("button");
        btn.className = "remove-wp";
        btn.textContent = "✕";
        btn.title = `Remove waypoint ${idx + 1}`;
        btn.setAttribute("aria-label", `Remove waypoint ${idx + 1}`);
        btn.type = "button";
        btn.addEventListener("click", () => removeWaypoint(idx));
        li.appendChild(btn);
        waypointsList.appendChild(li);
      });
    }
    redrawRoute();
    btnRouteStart.disabled = !state.connected || state.waypoints.length < 2;
  }

  function redrawRoute() {
    state.waypointMarkers.forEach((m) => map.removeLayer(m));
    state.waypointMarkers = [];

    if (state.routePolyline) {
      map.removeLayer(state.routePolyline);
      state.routePolyline = null;
    }

    state.waypoints.forEach(([lat, lon], idx) => {
      const marker = L.circleMarker([lat, lon], {
        radius: 6,
        weight: 2,
        color: idx === 0 ? "#2ec27e" : idx === state.waypoints.length - 1 ? "#ff5b5b" : "#4f8cff",
        fillColor: "#0f1115",
        fillOpacity: 1,
      })
        .bindTooltip(`#${idx + 1}`, { permanent: false, direction: "top" })
        .addTo(map);
      state.waypointMarkers.push(marker);
    });

    if (state.waypoints.length >= 2) {
      state.routePolyline = L.polyline(state.waypoints, {
        color: "#4f8cff",
        weight: 4,
        opacity: 0.7,
        dashArray: "6,8",
      }).addTo(map);
    }
  }

  function addWaypoint(lat, lon) {
    state.waypoints.push([lat, lon]);
    renderWaypoints();
  }

  function removeWaypoint(idx) {
    state.waypoints.splice(idx, 1);
    renderWaypoints();
  }

  function clearWaypoints() {
    state.waypoints = [];
    renderWaypoints();
  }

  // ---------------------------------------------------------------------
  // Location marker
  // ---------------------------------------------------------------------
  function showLocationMarker(lat, lon, label = "Set location") {
    if (state.locationMarker) map.removeLayer(state.locationMarker);
    state.locationMarker = L.marker([lat, lon], {
      title: label,
    })
      .addTo(map)
      .bindPopup(`<strong>${label}</strong><br>${fmtCoord(lat)}, ${fmtCoord(lon)}`);
  }

  function showCursorMarker(lat, lon) {
    if (state.cursorMarker) {
      state.cursorMarker.setLatLng([lat, lon]);
    } else {
      state.cursorMarker = L.circleMarker([lat, lon], {
        radius: 8,
        color: "#ff8a4c",
        weight: 3,
        fillColor: "#ff8a4c",
        fillOpacity: 0.5,
      }).addTo(map);
    }
  }

  function clearCursorMarker() {
    if (state.cursorMarker) {
      map.removeLayer(state.cursorMarker);
      state.cursorMarker = null;
    }
  }

  // ---------------------------------------------------------------------
  // Device actions
  // ---------------------------------------------------------------------
  async function refreshStatus() {
    try {
      const info = await api("/api/device/status");
      setDeviceUi(Boolean(info.connected), info);
    } catch (err) {
      console.warn("status failed", err);
    }
  }

  btnConnect.addEventListener("click", async () => {
    btnConnect.disabled = true;
    statusDot.classList.add("dot-busy");
    statusText.textContent = "Connecting…";
    try {
      const info = await api("/api/device/connect", {
        method: "POST",
        body: JSON.stringify({}),
      });
      setDeviceUi(Boolean(info.connected), info);
      toast("Connected to device.", "success");
    } catch (err) {
      setDeviceUi(false, null);
      toast(err.message, "error");
    } finally {
      statusDot.classList.remove("dot-busy");
    }
  });

  btnDisconnect.addEventListener("click", async () => {
    try {
      await api("/api/device/disconnect", { method: "POST", body: "{}" });
      setDeviceUi(false, null);
      toast("Disconnected.");
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // ---------------------------------------------------------------------
  // Set / Reset
  // ---------------------------------------------------------------------
  btnSet.addEventListener("click", async () => {
    const coords = readCoordInputs();
    if (!coords) {
      toast("Enter valid latitude (-90..90) and longitude (-180..180).", "error");
      return;
    }
    try {
      await api("/api/location/set", {
        method: "POST",
        body: JSON.stringify(coords),
      });
      showLocationMarker(coords.lat, coords.lon, "Spoofed location");
      map.panTo([coords.lat, coords.lon]);
      toast(`Location set: ${fmtCoord(coords.lat)}, ${fmtCoord(coords.lon)}`, "success");
      stopRoutePolling();
      hideRouteStatus();
    } catch (err) {
      toast(err.message, "error");
    }
  });

  btnReset.addEventListener("click", async () => {
    try {
      await api("/api/location/reset", { method: "POST", body: "{}" });
      if (state.locationMarker) {
        map.removeLayer(state.locationMarker);
        state.locationMarker = null;
      }
      clearCursorMarker();
      stopRoutePolling();
      hideRouteStatus();
      toast("Location reset — device using real GPS again.", "success");
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // ---------------------------------------------------------------------
  // Search
  // ---------------------------------------------------------------------
  searchForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const query = searchInput.value.trim();
    if (!query) return;
    searchResults.innerHTML = '<li class="empty">Searching…</li>';
    try {
      const payload = await api(`/api/geocode?q=${encodeURIComponent(query)}`);
      renderSearchResults(payload.results || []);
    } catch (err) {
      toast(err.message, "error");
      searchResults.innerHTML = "";
    }
  });

  function renderSearchResults(results) {
    searchResults.innerHTML = "";
    if (results.length === 0) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "No results.";
      searchResults.appendChild(li);
      return;
    }
    results.forEach((result) => {
      const li = document.createElement("li");
      const title = document.createElement("div");
      title.textContent = result.name;
      const sub = document.createElement("small");
      sub.textContent = `${fmtCoord(result.lat)}, ${fmtCoord(result.lon)}`;
      li.appendChild(title);
      li.appendChild(sub);
      li.addEventListener("click", () => {
        setCoordInputs(result.lat, result.lon);
        map.setView([result.lat, result.lon], 15);
      });
      searchResults.appendChild(li);
    });
  }

  // ---------------------------------------------------------------------
  // Route controls
  // ---------------------------------------------------------------------
  speedPreset.addEventListener("change", () => {
    customSpeedWrap.hidden = speedPreset.value !== "custom";
  });

  btnRouteClear.addEventListener("click", () => {
    clearWaypoints();
  });

  btnRouteStart.addEventListener("click", async () => {
    if (state.waypoints.length < 2) {
      toast("Add at least two waypoints (shift-click the map).", "error");
      return;
    }
    const body = {
      waypoints: state.waypoints,
      loop: loopRoute.checked,
    };
    if (speedPreset.value === "custom") {
      const kmh = parseFloat(customSpeed.value);
      if (!Number.isFinite(kmh) || kmh <= 0) {
        toast("Enter a positive custom speed.", "error");
        return;
      }
      body.speedKmh = kmh;
    } else {
      body.preset = speedPreset.value;
    }
    try {
      const status = await api("/api/route/start", {
        method: "POST",
        body: JSON.stringify(body),
      });
      btnRouteStop.disabled = false;
      btnRouteStart.disabled = true;
      toast("Route started.", "success");
      updateRouteStatus(status);
      startRoutePolling();
    } catch (err) {
      toast(err.message, "error");
    }
  });

  btnRouteStop.addEventListener("click", async () => {
    try {
      const status = await api("/api/route/stop", { method: "POST", body: "{}" });
      btnRouteStop.disabled = true;
      btnRouteStart.disabled = !state.connected || state.waypoints.length < 2;
      stopRoutePolling();
      updateRouteStatus(status);
      toast("Route stopped.");
    } catch (err) {
      toast(err.message, "error");
    }
  });

  // ---------------------------------------------------------------------
  // Route polling
  // ---------------------------------------------------------------------
  function startRoutePolling() {
    stopRoutePolling();
    state.routePolling = setInterval(async () => {
      try {
        const status = await api("/api/route/status");
        updateRouteStatus(status);
        if (!status.running) {
          stopRoutePolling();
          btnRouteStop.disabled = true;
          btnRouteStart.disabled = !state.connected || state.waypoints.length < 2;
        }
      } catch (err) {
        // network blip — keep polling
      }
    }, 1000);
  }

  function stopRoutePolling() {
    if (state.routePolling) {
      clearInterval(state.routePolling);
      state.routePolling = null;
    }
  }

  function hideRouteStatus() {
    routeStatusEl.hidden = true;
    routeStatusEl.innerHTML = "";
  }

  function updateRouteStatus(status) {
    if (!status || (!status.running && !status.finished && (!status.current))) {
      hideRouteStatus();
      return;
    }
    routeStatusEl.hidden = false;
    const lines = [];
    const stateLabel = status.running ? "Running" : status.finished ? "Finished" : "Stopped";
    lines.push(`<strong>${stateLabel}</strong>`);
    if (status.current) {
      const [lat, lon] = status.current;
      lines.push(`Position: ${fmtCoord(lat)}, ${fmtCoord(lon)}`);
      showCursorMarker(lat, lon);
    }
    if (typeof status.speedKmh === "number" && status.speedKmh > 0) {
      lines.push(`Speed: ${status.speedKmh.toFixed(1)} km/h`);
    }
    if (typeof status.totalDistanceM === "number" && status.totalDistanceM > 0) {
      const pct = Math.min(100, Math.round((100 * (status.travelledM || 0)) / status.totalDistanceM));
      lines.push(
        `Progress: ${fmtDistance(status.travelledM || 0)} of ${fmtDistance(status.totalDistanceM)} (${pct}%)`
      );
    }
    if (typeof status.elapsedS === "number") {
      lines.push(`Elapsed: ${fmtDuration(status.elapsedS)}`);
    }
    routeStatusEl.innerHTML = lines.map((l) => `<div>${l}</div>`).join("");
  }

  // ---------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------
  renderWaypoints();
  refreshStatus();
  // Refresh status periodically in case device gets disconnected externally.
  setInterval(refreshStatus, 5000);
})();
