const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function createDashboard() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        hidden: false,
        disabled: false,
        textContent: "",
        listeners: {},
        classList: { toggle() {}, add() {}, remove() {} },
        addEventListener(name, callback) { this.listeners[name] = callback; },
        setAttribute() {},
        getAttribute() { return "6"; },
        getBoundingClientRect() { return { width: 400 }; },
        replaceChildren() {},
        appendChild() {},
      });
    }
    return elements.get(id);
  }
  const intervals = new Map();
  let nextInterval = 0;
  const setInterval = (callback, delay) => {
    const id = ++nextInterval;
    intervals.set(id, { callback, delay });
    return id;
  };
  class AudioContext {
    constructor() {
      this.state = "running";
      this.currentTime = 0;
      this.destination = {};
    }
    createOscillator() {
      return {
        frequency: { value: 0 },
        connect() {},
        disconnect() {},
        start() {},
        stop() { this.stopped = true; },
      };
    }
    createGain() {
      return {
        gain: {
          value: 0,
          scheduled: [],
          setValueAtTime(value, time) { this.scheduled.push({ value, time }); },
        },
        connect() {},
        disconnect() {},
      };
    }
    async resume() { this.state = "running"; }
  }
  const server = {
    fall: { active: false, reason: null, simulate_enabled: true },
    liveStatus: 200,
    dismissStatus: 200,
    delayedLive: null,
  };
  const response = (status, data) => ({
    ok: status >= 200 && status < 300,
    status,
    async json() { return data; },
  });
  async function fetch(url) {
    if (url === "/api/stats") {
      return response(200, {
        routes_discovered: 0, crux_peak: 0, longest_ascent: 0,
        terrain_stability: 0, sessions: [],
      });
    }
    if (url === "/api/fall/simulate") {
      server.fall = { active: true, reason: "Simulated fall (demo)", simulate_enabled: true };
      return response(200, { fall: server.fall });
    }
    if (url === "/api/fall/dismiss") {
      if (server.dismissStatus !== 200) return response(server.dismissStatus, { error: "Dismiss failed" });
      server.fall = { active: false, reason: null, simulate_enabled: true };
      return response(200, { fall: server.fall });
    }
    if (url === "/api/live") {
      if (server.delayedLive) {
        const delayed = server.delayedLive;
        server.delayedLive = null;
        return delayed;
      }
      return response(server.liveStatus, {
        fall: server.fall, error: "Telemetry offline", recording: false,
        elapsed: 0, pr: 1, battery: 80,
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  }
  const context = vm.createContext({
    ROUTE: [],
    document: {
      getElementById: element,
      querySelectorAll: () => [],
      body: element("body"),
    },
    window: { AudioContext, setInterval, clearInterval: id => intervals.delete(id) },
    setInterval,
    fetch,
    console: { error() {} },
  });
  const script = fs.readFileSync(path.join(__dirname, "static", "app.js"), "utf8");
  vm.runInContext(script, context);
  return { context, element, server, response, intervals };
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test("simulate enables audio, displays alert, and dismiss stops sound", async () => {
  const { context, element, intervals } = createDashboard();
  await settle();
  element("simulateFallBtn").listeners.click();
  await settle();
  assert.equal(element("fallAlert").hidden, false);
  assert.equal(element("fallReason").textContent, "Simulated fall (demo)");
  assert.equal(vm.runInContext("alertOscillator.frequency.value", context), 880);
  const beepTimer = vm.runInContext("beepTimer", context);
  assert.equal(intervals.get(beepTimer).delay, 500);
  assert.equal(vm.runInContext("alertGain.gain.scheduled[0].value", context), 0.12);
  assert.equal(vm.runInContext("alertGain.gain.scheduled[1].value", context), 0);
  assert.equal(vm.runInContext("alertGain.gain.scheduled[1].time", context), 0.2);
  element("dismissFallBtn").listeners.click();
  await settle();
  assert.equal(element("fallAlert").hidden, true);
  assert.equal(vm.runInContext("alertOscillator", context), null);
  assert.equal(vm.runInContext("beepTimer", context), null);
});

test("simulation still displays a warning when browser audio is unavailable", async () => {
  const { context, element } = createDashboard();
  await settle();
  vm.runInContext("window.AudioContext = undefined", context);
  element("simulateFallBtn").listeners.click();
  await settle();
  assert.equal(element("fallAlert").hidden, false);
  assert.equal(element("fallSoundWarning").hidden, false);
  assert.match(element("soundStatus").textContent, /Unable to enable/);
});

test("503 preserves fall alert and a failed dismiss is visible", async () => {
  const { context, element, server } = createDashboard();
  await settle();
  server.liveStatus = 503;
  server.fall = { active: true, reason: "Camera alert", simulate_enabled: false };
  await vm.runInContext("fetchLive()", context);
  assert.equal(element("fallAlert").hidden, false);
  assert.equal(element("fallReason").textContent, "Camera alert");
  assert.equal(element("simulateFallBtn").hidden, true);
  assert.equal(element("liveError").textContent, "Telemetry offline");
  server.dismissStatus = 503;
  await vm.runInContext('changeFallState("/api/fall/dismiss")', context);
  assert.equal(element("fallControlError").textContent, "Dismiss failed");
  assert.equal(element("fallAlert").hidden, false);
});

test("an older live response cannot clear a newly simulated alert", async () => {
  const { context, element, server, response } = createDashboard();
  await settle();
  let resolveLive;
  server.delayedLive = new Promise(resolve => { resolveLive = resolve; });
  const oldLive = vm.runInContext("fetchLive()", context);
  await vm.runInContext('changeFallState("/api/fall/simulate")', context);
  resolveLive(response(200, { fall: { active: false, simulate_enabled: true } }));
  await oldLive;
  assert.equal(element("fallAlert").hidden, false);
  assert.equal(element("fallReason").textContent, "Simulated fall (demo)");
});
