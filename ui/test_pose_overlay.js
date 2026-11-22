const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const PoseOverlay = require(path.join(__dirname, "static", "pose-overlay.js"));
const { interpolatePose, coverTransform, fromKeypoints, createOverlay } = PoseOverlay;

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test("interpolatePose moves shared points with ease-out", () => {
  const from = { nose: { x: 0, y: 0, a: 1 } };
  const to = { nose: { x: 100, y: 50, a: 1 } };
  assert.deepEqual(interpolatePose(from, to, 0).nose, { x: 0, y: 0, a: 1 });
  assert.deepEqual(interpolatePose(from, to, 1).nose, { x: 100, y: 50, a: 1 });
  const mid = interpolatePose(from, to, 0.5).nose;
  close(mid.x, 87.5);  // easeOutCubic(0.5) = 0.875: fast start, slow settle
  close(mid.y, 43.75);
});

test("points fade in where they appear and fade out where they were", () => {
  const from = { left_wrist: { x: 10, y: 10, a: 1 } };
  const to = { right_wrist: { x: 90, y: 90, a: 1 } };
  const mid = interpolatePose(from, to, 0.5);
  assert.equal(mid.right_wrist.x, 90);
  close(mid.right_wrist.a, 0.875);
  assert.equal(mid.left_wrist.x, 10);
  close(mid.left_wrist.a, 0.125);
  const end = interpolatePose(from, to, 1);
  assert.equal(end.left_wrist, undefined);
  assert.equal(end.right_wrist.a, 1);
});

test("t is clamped", () => {
  const from = { nose: { x: 0, y: 0, a: 1 } };
  const to = { nose: { x: 10, y: 0, a: 1 } };
  assert.equal(interpolatePose(from, to, 5).nose.x, 10);
  assert.equal(interpolatePose(from, to, -1).nose.x, 0);
});

test("fromKeypoints keeps only visible finite points", () => {
  const pose = fromKeypoints({
    nose: { x: 1, y: 2, visible: true },
    left_eye: { x: 3, y: 4, visible: false },
    right_eye: { x: null, y: 4, visible: true },
  });
  assert.deepEqual(pose, { nose: { x: 1, y: 2, a: 1 } });
  assert.deepEqual(fromKeypoints(null), {});
});

test("coverTransform crops the overflowing axis and centres it", () => {
  // 640x480 image in a wider 800x400 box: width fills, height overflows
  let fit = coverTransform(640, 480, 800, 400);
  close(fit.scale, 1.25);
  close(fit.ox, 0);
  close(fit.oy, (400 - 600) / 2);
  // taller box: height fills, width overflows
  fit = coverTransform(640, 480, 300, 600);
  close(fit.scale, 1.25);
  close(fit.ox, (300 - 800) / 2);
  close(fit.oy, 0);
});

function harness(responses) {
  let t = 0;
  const queue = [...responses];
  const overlay = createOverlay({
    canvas: { clientWidth: 0, clientHeight: 0, getContext: () => null },
    img: { naturalWidth: 0, naturalHeight: 0 },
    now: () => t,
    requestAnimationFrame: () => 1,
    cancelAnimationFrame: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    fetch: async () => {
      const next = queue.shift();
      if (next instanceof Error) throw next;
      return { ok: true, json: async () => next };
    },
  });
  return { overlay, advance: ms => { t += ms; }, queue };
}

const sample = (ts, x, extra = {}) => ({
  timestamp: ts, enabled: true, detected: true, frame_shape: [480, 640],
  keypoints: { nose: { x, y: 0, visible: true } }, ...extra,
});

test("new samples retarget from the on-screen pose without snapping", async () => {
  const { overlay, advance, queue } = harness([sample(1, 0)]);
  overlay.start();
  await new Promise(r => setImmediate(r));
  advance(1000);
  assert.equal(overlay._current().nose.x, 0);

  queue.push(sample(2, 100));
  advance(450);
  await overlay._poll();
  assert.equal(overlay._current().nose.x, 0);  // starts where it was
  advance(10_000);
  assert.equal(overlay._current().nose.x, 100);

  // retarget mid-tween: continues from the in-between position
  queue.push(sample(3, 200));
  await overlay._poll();
  advance(overlay._interval / 2);
  const midway = overlay._current().nose.x;
  queue.push(sample(4, 0));
  await overlay._poll();
  close(overlay._current().nose.x, midway);
  overlay.stop();
});

test("repeated timestamps are ignored and the interval tracks update spacing", async () => {
  const { overlay, advance, queue } = harness([sample(1, 0)]);
  overlay.start();
  await new Promise(r => setImmediate(r));
  for (let i = 2; i < 30; i++) {
    advance(100);
    queue.push(sample(i - 1, 0));  // same timestamp as before: no update
    await overlay._poll();
    advance(350);
    queue.push(sample(i, 0));
    await overlay._poll();
  }
  close(overlay._interval, 450, 1);
  overlay.stop();
});

test("paused and lost detections fade the skeleton out", async () => {
  const { overlay, advance, queue } = harness([sample(1, 50)]);
  overlay.start();
  await new Promise(r => setImmediate(r));
  advance(1000);
  queue.push(sample(2, 50, { detected: false, keypoints: null }));
  await overlay._poll();
  advance(300);
  assert.equal(overlay._current().nose, undefined);

  queue.push(sample(3, 50, { enabled: false, detected: false }));
  await overlay._poll();
  assert.equal(overlay._paused, true);
  overlay.stop();
});
