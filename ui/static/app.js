const routeLine = document.getElementById("routeLine");
const routeNodes = document.getElementById("routeNodes");
const chartSvg = document.getElementById("chart");
const chartLine = document.getElementById("line");
const chartArea = document.getElementById("area");
const chartHead = document.getElementById("head");
const intensityHistory = [];
const fallAlert = document.getElementById("fallAlert");
const fallReason = document.getElementById("fallReason");
const fallSoundWarning = document.getElementById("fallSoundWarning");
const soundStatus = document.getElementById("soundStatus");
const enableSoundBtn = document.getElementById("enableSoundBtn");
const simulateFallBtn = document.getElementById("simulateFallBtn");
const dismissFallBtn = document.getElementById("dismissFallBtn");
const fallControlError = document.getElementById("fallControlError");
const liveError = document.getElementById("liveError");
let audioContext = null;
let alertOscillator = null;
let alertGain = null;
let beepTimer = null;
let soundFailed = false;
let soundError = null;
let fallActive = false;
let fallActionPending = false;
let fallVersion = 0;
let liveRequest = 0;
let lastLiveResponse = 0;

function stopAlertSound() {
  if (beepTimer !== null) {
    window.clearInterval(beepTimer);
    beepTimer = null;
  }
  if (alertOscillator) {
    alertOscillator.stop();
    alertOscillator.disconnect();
    alertOscillator = null;
  }
  if (alertGain) {
    alertGain.disconnect();
    alertGain = null;
  }
}

function syncAlertSound() {
  const soundReady = audioContext && audioContext.state === "running" && !soundFailed;
  fallSoundWarning.hidden = Boolean(soundReady);
  enableSoundBtn.textContent = soundReady ? "Alert sound enabled" : "Enable alert sound";
  soundStatus.textContent = soundReady
    ? "Alert sound enabled."
    : (soundError || "Alert sound is off until enabled.");
  if (!fallActive || !soundReady) {
    stopAlertSound();
    return;
  }
  if (alertOscillator) return;

  try {
    const oscillator = audioContext.createOscillator();
    const gain = audioContext.createGain();
    oscillator.frequency.value = 880;
    gain.gain.value = 0;
    oscillator.connect(gain);
    gain.connect(audioContext.destination);
    oscillator.start();
    alertOscillator = oscillator;
    alertGain = gain;
    const beep = () => {
      const now = audioContext.currentTime;
      gain.gain.setValueAtTime(0.12, now);
      gain.gain.setValueAtTime(0, now + 0.2);
    };
    beep();
    beepTimer = window.setInterval(beep, 500);
  } catch (error) {
    console.error(error);
    stopAlertSound();
    soundFailed = true;
    soundError = "Unable to play alert sound. Try enabling it again.";
    fallSoundWarning.hidden = false;
    enableSoundBtn.textContent = "Enable alert sound";
    soundStatus.textContent = soundError;
  }
}

async function enableAlertSound() {
  try {
    soundFailed = false;
    soundError = null;
    if (!audioContext) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) throw new Error("Web Audio is not supported by this browser.");
      audioContext = new AudioContext();
      audioContext.onstatechange = syncAlertSound;
    }
    if (audioContext.state !== "running") await audioContext.resume();
    if (audioContext.state !== "running") throw new Error("Browser did not enable audio.");
    syncAlertSound();
  } catch (error) {
    console.error(error);
    soundFailed = true;
    soundError = "Unable to enable alert sound. Check browser audio settings.";
    syncAlertSound();
  }
}

function showFall(fall) {
  if (!fall || typeof fall.active !== "boolean") return;
  fallActive = fall.active;
  fallAlert.hidden = !fallActive;
  document.body.classList.toggle("fall-alert-active", fallActive);
  const reason = fallActive ? (fall.reason || "Please check on the climber.") : "";
  if (fallReason.textContent !== reason) fallReason.textContent = reason;
  if (typeof fall.simulate_enabled === "boolean") {
    simulateFallBtn.hidden = !fall.simulate_enabled;
  }
  syncAlertSound();
}

async function changeFallState(endpoint) {
  if (fallActionPending) return;
  fallActionPending = true;
  fallVersion++;
  dismissFallBtn.disabled = true;
  simulateFallBtn.disabled = true;
  fallControlError.hidden = true;
  try {
    const response = await fetch(endpoint, { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Fall API returned ${response.status}`);
    showFall(data.fall);
  } catch (error) {
    console.error(error);
    fallControlError.textContent = error.message || "Unable to update fall alert.";
    fallControlError.hidden = false;
  } finally {
    fallActionPending = false;
    fallVersion++;
    dismissFallBtn.disabled = false;
    simulateFallBtn.disabled = false;
    await fetchLive();
  }
}

function renderRoute() {
  if (!routeLine || !routeNodes) return;
  const points = ROUTE.map(([x, y]) => `${x * 100},${y * 100}`).join(" ");
  routeLine.setAttribute("points", points);
  routeNodes.replaceChildren();
  ROUTE.forEach(([x, y]) => {
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", x * 100);
    circle.setAttribute("cy", y * 100);
    circle.setAttribute("r", 1.8);
    routeNodes.appendChild(circle);
  });
}

function formatElapsed(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return [hours, minutes, secs].map(value => String(value).padStart(2, "0")).join(":");
}

function renderChart() {
  if (!chartLine || !chartArea) return;
  const width = 400;
  const height = 80;
  const leftInset = chartHead ? Number(chartHead.getAttribute("r")) + 4 : 0;
  const chartWidth = chartSvg.getBoundingClientRect().width;
  const requestedShift = chartWidth > 0 ? 40 * width / chartWidth : 0;
  const rightInset = leftInset + Math.min(requestedShift, width - 2 * leftInset - 1);
  const plotWidth = width - leftInset - rightInset;
  const values = intensityHistory.length ? intensityHistory : [0.5];
  const points = values.map((value, index) => {
    const x = leftInset + (values.length === 1 ? 0 : index * plotWidth / (values.length - 1));
    const y = height - Math.max(0, Math.min(1, value)) * (height - 8) - 4;
    return [x, y];
  });
  const line = points.map(([x, y]) => `${x},${y}`).join(" ");
  chartLine.setAttribute("points", line);
  chartArea.setAttribute("d", `M0,${height} L${points.map(([x, y]) => `${x},${y}`).join(" L")} L${width},${height} Z`);
  if (chartHead && points.length) {
    const [x, y] = points[points.length - 1];
    chartHead.setAttribute("cx", x);
    chartHead.setAttribute("cy", y);
  }
}

async function fetchLive() {
  const request = ++liveRequest;
  const version = fallVersion;
  try {
    const response = await fetch("/api/live");
    const data = await response.json();
    if (request < lastLiveResponse) return;
    lastLiveResponse = request;
    if (!fallActionPending && version === fallVersion) showFall(data.fall);
    if (typeof data.stream_connected === "boolean") updateVideoState(data.stream_connected);
    if (!response.ok) throw new Error(data.error || `Live API returned ${response.status}`);
    liveError.hidden = true;

    document.getElementById("bpm").textContent = data.bpm ?? "--";
    document.getElementById("pr").textContent = data.pr;
    document.getElementById("battText").textContent = `${data.battery}%`;
    document.getElementById("battFill").setAttribute("width", String(18 * data.battery / 100));
    document.getElementById("time").textContent = formatElapsed(data.elapsed);
    document.getElementById("camTime").textContent = formatElapsed(data.elapsed);

    const recording = Boolean(data.recording);
    document.getElementById("recText").textContent = recording ? "Recording…" : "Paused";
    document.getElementById("recBtn").classList.toggle("off", !recording);

    if (recording && Number.isFinite(data.intensity)) {
      intensityHistory.push(Math.max(0, Math.min(1, data.intensity)));
      if (intensityHistory.length > 50) intensityHistory.shift();
      renderChart();
    }
  } catch (error) {
    console.error(error);
    if (request >= lastLiveResponse) {
      lastLiveResponse = request;
      liveError.textContent = error.message || "Unable to load live telemetry.";
      liveError.hidden = false;
    }
  }
}

function updateVideoState(connected) {
  const videoContainer = document.getElementById("videoContainer");
  const disconnectedMessage = document.getElementById("disconnectedMessage");
  const videoStream = document.getElementById("videoStream");

  if (connected) {
    videoContainer.hidden = false;
    disconnectedMessage.hidden = true;
    // Only set src if not already set to avoid re-loading
    if (!videoStream.getAttribute("src")) {
      videoStream.src = "/video/stream";
    }
  } else {
    videoContainer.hidden = true;
    disconnectedMessage.hidden = false;
    videoStream.removeAttribute("src");
  }
}

function handleVideoError() {
  updateVideoState(false);
}

async function fetchStats() {
  try {
    const response = await fetch("/api/stats");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Stats API returned ${response.status}`);
    document.getElementById("sSessions").textContent = data.routes_discovered;
    document.getElementById("sPeak").textContent = data.crux_peak;
    document.getElementById("sLongest").textContent = formatElapsed(data.longest_ascent);
    document.getElementById("sStability").textContent = data.terrain_stability;
    document.getElementById("statsError").hidden = true;
    document.getElementById("statsEmpty").hidden = data.sessions.length > 0;
    renderExpeditionLog(data.sessions);
  } catch (error) {
    console.error(error);
    document.getElementById("statsError").textContent = error.message || "Unable to load session telemetry. Please try again.";
    document.getElementById("statsError").hidden = false;
  }
}

function renderExpeditionLog(sessions) {
  const log = document.getElementById("expeditionLog");
  log.replaceChildren();

  sessions.forEach((session, index) => {
    const card = document.createElement("article");
    card.className = "expedition-card";

    const header = document.createElement("div");
    header.className = "expedition-card-header";
    const title = document.createElement("h3");
    title.textContent = `Expedition ${sessions.length - index}`;
    const date = document.createElement("time");
    date.textContent = new Date(session.started_at).toLocaleString();
    header.append(title, date);
    card.appendChild(header);

    const metrics = document.createElement("dl");
    metrics.className = "expedition-metrics";
    [
      ["Duration", formatElapsed(session.duration_sec)],
      ["Avg BPM", session.avg_bpm == null ? "—" : `${session.avg_bpm} BPM`],
      ["Peak BPM", session.peak_bpm == null ? "—" : `${session.peak_bpm} BPM`],
      ["Hold Shifts", session.hold_shifts],
      ["Stability", `${session.stability_pct}%`],
    ].forEach(([label, value]) => {
      const item = document.createElement("div");
      const term = document.createElement("dt");
      const detail = document.createElement("dd");
      term.textContent = label;
      detail.textContent = value;
      item.append(term, detail);
      metrics.appendChild(item);
    });
    card.appendChild(metrics);

    const report = document.createElement("div");
    report.className = "field-report";
    const reportTitle = document.createElement("h4");
    reportTitle.textContent = "Gemini Field Report";
    const reportText = document.createElement("p");
    reportText.className = "field-report-text";
    reportText.textContent = session.summary || "No field report saved for this climb yet.";
    report.append(reportTitle, reportText);
    if (!session.summary) {
      const button = document.createElement("button");
      button.className = "field-report-button";
      button.type = "button";
      button.dataset.sessionId = session.id;
      button.textContent = "Generate Field Report";
      report.appendChild(button);
    }
    card.appendChild(report);
    log.appendChild(card);
  });
}

document.getElementById("expeditionLog").addEventListener("click", async event => {
  const button = event.target.closest(".field-report-button");
  if (!button) return;

  const report = button.closest(".field-report");
  const reportText = report.querySelector(".field-report-text");
  button.disabled = true;
  button.textContent = "Generating…";
  report.classList.remove("has-error");
  try {
    const response = await fetch(
      `/api/sessions/${encodeURIComponent(button.dataset.sessionId)}/summary`,
      { method: "POST" }
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Report API returned ${response.status}`);
    reportText.textContent = data.summary;
    button.remove();
  } catch (error) {
    console.error(error);
    reportText.textContent = error.message || "Unable to generate the field report.";
    report.classList.add("has-error");
    button.disabled = false;
    button.textContent = "Try Again";
  }
});

function showTab(name) {
  document.querySelectorAll(".tab").forEach(tab => tab.classList.remove("active"));
  document.querySelectorAll(".nav button").forEach(button => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.getElementById(`tab-${name}`).classList.add("active");
  if (name === "stats") fetchStats();
}

document.querySelectorAll(".nav button").forEach(button => {
  button.addEventListener("click", () => showTab(button.dataset.tab));
});

enableSoundBtn.addEventListener("click", enableAlertSound);
simulateFallBtn.addEventListener("click", () => {
  void enableAlertSound();
  void changeFallState("/api/fall/simulate");
});
dismissFallBtn.addEventListener("click", () => {
  void changeFallState("/api/fall/dismiss");
});

document.getElementById("recBtn").addEventListener("click", async () => {
  void enableAlertSound();
  try {
    const response = await fetch("/api/recording/toggle", { method: "POST" });
    if (!response.ok) throw new Error(`Recording API returned ${response.status}`);
    const data = await response.json();
    await fetchLive();
  } catch (error) {
    console.error(error);
  }
});

document.getElementById("replayBtn").addEventListener("click", () => {
  intensityHistory.splice(0, intensityHistory.length);
  renderChart();
  let frame = 0;
  const replayValues = [0.4, 0.48, 0.62, 0.55, 0.75, 0.68, 0.82, 0.58, 0.46, 0.72];
  const interval = window.setInterval(() => {
    if (frame >= replayValues.length) {
      window.clearInterval(interval);
      return;
    }
    intensityHistory.push(replayValues[frame++]);
    renderChart();
  }, 180);
});

renderRoute();
renderChart();
fetchLive();
fetchStats();
setInterval(fetchLive, 1000);