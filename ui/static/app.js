const routeLine = document.getElementById("routeLine");
const routeNodes = document.getElementById("routeNodes");
const chartSvg = document.getElementById("chart");
const chartLine = document.getElementById("line");
const chartArea = document.getElementById("area");
const chartHead = document.getElementById("head");
const intensityHistory = [];

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
  try {
    const response = await fetch("/api/live");
    if (!response.ok) throw new Error(`Live API returned ${response.status}`);
    const data = await response.json();

    document.getElementById("bpm").textContent = data.bpm;
    document.getElementById("pr").textContent = data.pr;
    document.getElementById("battText").textContent = `${data.battery}%`;
    document.getElementById("battFill").setAttribute("width", String(18 * data.battery / 100));
    document.getElementById("time").textContent = formatElapsed(data.elapsed);
    document.getElementById("camTime").textContent = formatElapsed(data.elapsed);

    const recording = Boolean(data.recording);
    document.getElementById("recText").textContent = recording ? "Recording…" : "Paused";
    document.getElementById("recBtn").classList.toggle("off", !recording);

    if (recording) {
      intensityHistory.push(Math.max(0, Math.min(1, data.intensity)));
      if (intensityHistory.length > 50) intensityHistory.shift();
      renderChart();
    }
  } catch (error) {
    console.error(error);
  }
}

async function fetchStats() {
  try {
    const response = await fetch("/api/stats");
    if (!response.ok) throw new Error(`Stats API returned ${response.status}`);
    const data = await response.json();
    document.getElementById("sSessions").textContent = data.sessions;
    document.getElementById("sBest").textContent = data.best;
    document.getElementById("sAvg").textContent = data.avg;
    document.getElementById("sBpm").textContent = data.avg_bpm;
  } catch (error) {
    console.error(error);
  }
}

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

document.getElementById("recBtn").addEventListener("click", async () => {
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