/*
 * Pose skeleton overlay for the raw camera feed.
 *
 * The QNX pose model only produces ~2 updates a second, but the raw camera
 * video plays at the camera's frame rate. This draws the skeleton on a canvas
 * over the video every animation frame (~60 Hz), tweening keypoints from where
 * they are on screen to each new pose update so the motion is smooth instead
 * of jumping twice a second.
 *
 * Keypoints from /api/pose are in un-mirrored frame pixels (frame_shape). The
 * canvas sits exactly over the <img> (object-fit: cover) and is mirrored with
 * it by CSS, so no flipping happens here except for text.
 */
(function (root) {
  "use strict";

  // COCO-17, same order as pose-landmarks/src/recognition.py KEYPOINT_NAMES
  const NAMES = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
  ];
  // Colours match pose-landmarks/src/framebox.py SKELETON (BGR there, RGB here)
  const HEAD = "rgb(255,255,0)", ARM = "rgb(255,165,0)", TORSO = "rgb(0,255,0)", LEG = "rgb(255,0,255)";
  const SKELETON = [
    [0, 1, HEAD], [0, 2, HEAD], [1, 3, HEAD], [2, 4, HEAD], [3, 5, HEAD], [4, 6, HEAD],
    [5, 7, ARM], [7, 9, ARM], [6, 8, ARM], [8, 10, ARM],
    [5, 6, TORSO], [5, 11, TORSO], [6, 12, TORSO], [11, 12, TORSO],
    [11, 13, LEG], [13, 15, LEG], [12, 14, LEG], [14, 16, LEG],
  ];

  const MIN_INTERVAL_MS = 150;
  const MAX_INTERVAL_MS = 800;
  const FADE_MS = 300;

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const lerp = (a, b, t) => a + (b - a) * t;
  const easeOutCubic = t => 1 - Math.pow(1 - t, 3);

  /** Server keypoints {name: {x, y, visible}} -> pose {name: {x, y, a}} (a = opacity). */
  function fromKeypoints(keypoints) {
    const pose = {};
    if (!keypoints) return pose;
    for (const name of NAMES) {
      const k = keypoints[name];
      if (k && k.visible && Number.isFinite(k.x) && Number.isFinite(k.y)) {
        pose[name] = { x: k.x, y: k.y, a: 1 };
      }
    }
    return pose;
  }

  /**
   * Pose between `from` and `to` at progress t (0..1, eased).
   * Points in both are moved; points only in `to` fade in where they appear;
   * points only in `from` fade out where they were.
   */
  function interpolatePose(from, to, t) {
    const e = easeOutCubic(clamp(t, 0, 1));
    const out = {};
    for (const name of NAMES) {
      const f = from && from[name];
      const g = to && to[name];
      const fa = f ? f.a : 0;
      const ga = g ? g.a : 0;
      if (fa > 0 && ga > 0) {
        out[name] = { x: lerp(f.x, g.x, e), y: lerp(f.y, g.y, e), a: lerp(fa, ga, e) };
      } else if (ga > 0) {
        out[name] = { x: g.x, y: g.y, a: ga * e };
      } else if (fa > 0 && e < 1) {
        out[name] = { x: f.x, y: f.y, a: fa * (1 - e) };
      }
    }
    return out;
  }

  /** object-fit: cover mapping from image pixels to container pixels. */
  function coverTransform(imgW, imgH, boxW, boxH) {
    const scale = Math.max(boxW / imgW, boxH / imgH);
    return { scale, ox: (boxW - imgW * scale) / 2, oy: (boxH - imgH * scale) / 2 };
  }

  function createOverlay(options) {
    const canvas = options.canvas;
    const img = options.img;
    const fetchFn = options.fetch || ((...a) => root.fetch(...a));
    const now = options.now || (() => performance.now());
    const raf = options.requestAnimationFrame || (cb => root.requestAnimationFrame(cb));
    const caf = options.cancelAnimationFrame || (id => root.cancelAnimationFrame(id));
    const setTimer = options.setInterval || ((cb, ms) => root.setInterval(cb, ms));
    const clearTimer = options.clearInterval || (id => root.clearInterval(id));
    const pollMs = options.pollMs || 100;
    const mirrored = options.mirrored !== false;

    let running = false, timer = null, frame = null;
    let prev = {}, next = {}, tStart = 0, duration = FADE_MS;
    let interval = 450, lastTs = null, lastNewAt = null;
    let frameShape = null, paused = false, inFlight = false;

    const progress = t => clamp((t - tStart) / duration, 0, 1);
    const current = () => interpolatePose(prev, next, progress(now()));

    function retarget(target, ms) {
      prev = current();  // continue from what is on screen: no snapping
      next = target;
      duration = ms;
      tStart = now();
    }

    async function poll() {
      if (inFlight) return;
      inFlight = true;
      try {
        const response = await fetchFn("/api/pose");
        if (!running) return;
        if (!response.ok) throw new Error(`pose ${response.status}`);
        const data = await response.json();
        if (!running || data.timestamp === lastTs) return;
        const t = now();
        if (lastNewAt !== null) {
          interval = clamp(0.7 * interval + 0.3 * (t - lastNewAt), MIN_INTERVAL_MS, MAX_INTERVAL_MS);
        }
        lastNewAt = t;
        lastTs = data.timestamp;
        paused = data.enabled === false;
        if (Array.isArray(data.frame_shape)) frameShape = data.frame_shape;
        if (paused || !data.detected || !data.keypoints) {
          retarget({}, FADE_MS);
        } else {
          retarget(fromKeypoints(data.keypoints), interval);
        }
      } catch (error) {
        if (running && Object.keys(next).length) retarget({}, FADE_MS);
      } finally {
        inFlight = false;
      }
    }

    function draw() {
      const dpr = root.devicePixelRatio || 1;
      const cw = canvas.clientWidth, ch = canvas.clientHeight;
      const w = Math.round(cw * dpr), h = Math.round(ch * dpr);
      if (canvas.width !== w || canvas.height !== h) {
        canvas.width = w;
        canvas.height = h;
      }
      const ctx = canvas.getContext("2d");
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, w, h);

      if (paused) {
        // the canvas is mirrored by CSS; un-mirror so the text reads normally
        if (mirrored) ctx.setTransform(-1, 0, 0, 1, w, 0);
        ctx.font = `${14 * dpr}px system-ui, sans-serif`;
        ctx.fillStyle = "rgba(255,255,255,0.85)";
        ctx.fillText("tracking paused", 12 * dpr, h - 12 * dpr);
        return;
      }
      const iw = img.naturalWidth, ih = img.naturalHeight;
      if (!frameShape || !iw || !ih || !cw || !ch) return;

      const pose = current();
      const fit = coverTransform(iw, ih, cw, ch);
      // frame pixels -> image pixels -> container CSS pixels -> canvas pixels
      const sx = (iw / frameShape[1]) * fit.scale * dpr;
      const sy = (ih / frameShape[0]) * fit.scale * dpr;
      const px = p => [p.x * sx + fit.ox * dpr, p.y * sy + fit.oy * dpr];

      ctx.lineWidth = 3 * dpr;
      ctx.lineCap = "round";
      for (const [a, b, color] of SKELETON) {
        const pa = pose[NAMES[a]], pb = pose[NAMES[b]];
        if (!pa || !pb) continue;
        const [x1, y1] = px(pa), [x2, y2] = px(pb);
        ctx.globalAlpha = Math.min(pa.a, pb.a);
        ctx.strokeStyle = color;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
      ctx.fillStyle = "rgb(255,60,60)";
      for (const name of NAMES) {
        const p = pose[name];
        if (!p) continue;
        const [x, y] = px(p);
        ctx.globalAlpha = p.a;
        ctx.beginPath();
        ctx.arc(x, y, 4 * dpr, 0, 2 * Math.PI);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    }

    function loop() {
      if (!running) return;
      draw();
      frame = raf(loop);
    }

    return {
      start() {
        if (running) return;
        running = true;
        poll();
        timer = setTimer(poll, pollMs);
        frame = raf(loop);
      },
      stop() {
        running = false;
        if (timer !== null) clearTimer(timer);
        if (frame !== null) caf(frame);
        timer = frame = null;
        prev = {};
        next = {};
        lastTs = lastNewAt = null;
        paused = false;
        const ctx = canvas.getContext && canvas.getContext("2d");
        if (ctx) {
          ctx.setTransform(1, 0, 0, 1, 0, 0);
          ctx.clearRect(0, 0, canvas.width, canvas.height);
        }
      },
      get running() { return running; },
      // exposed for tests
      _poll: poll,
      _current: current,
      get _interval() { return interval; },
      get _paused() { return paused; },
    };
  }

  const api = { NAMES, SKELETON, fromKeypoints, interpolatePose, coverTransform, createOverlay };
  root.PoseOverlay = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
