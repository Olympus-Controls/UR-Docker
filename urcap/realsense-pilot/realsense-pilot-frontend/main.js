// RealSense Pilot — Application Node presenter: a plain custom element (no
// framework, no build). PolyScope X sets `applicationNode`, `applicationAPI`,
// `robotSettings` and `robotContext` on it (SDK 6.5.65 JavaScript template).
//
// The page talks to the RealSense cockpit (`perception gui --cors <this origin>`)
// over its HTTP API: the colour feed is `GET /api/color.png` long-polled by
// sequence number, hover reads `GET /api/point`, a click runs
// `POST /api/segment` → `POST /api/robot/locate` (hand-eye → base frame,
// approach pose, reach check), and the two Move buttons either hand the target
// to PolyScope (inverse kinematics + UR's own auto-move screen, operator in the
// loop) or ask the cockpit to move over Primary (urctl's safety envelope).
(() => {
  const TAG = "olympus-realsense-pilot";
  const DEFAULT_COCKPIT_PORT = 7621;
  const POLL_TIMEOUT_MS = 1500;
  const HOVER_MS = 150;
  const PROBE_TIMEOUT_MS = 2500;
  const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

  const CSS = `
    .rsp { font: 14px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; color: #1f2a37; padding: 12px 16px; max-width: 1100px; }
    .rsp h2 { margin: 0 0 8px; font-size: 18px; display: flex; align-items: center; gap: 10px; }
    .rsp .dot { width: 10px; height: 10px; border-radius: 50%; background: #c0c8d2; display: inline-block; }
    .rsp .dot.live { background: #1d9a5a; } .rsp .dot.dead { background: #d64545; }
    .rsp .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 6px 0; }
    .rsp input[type=text] { flex: 1 1 320px; min-width: 240px; padding: 8px 10px; border: 1px solid #b9c3cf; border-radius: 6px; font: inherit; }
    .rsp button { padding: 9px 14px; border: 1px solid #8e9bab; border-radius: 6px; background: #fff; font: inherit; cursor: pointer; }
    .rsp button:disabled { opacity: .45; cursor: default; }
    .rsp button.primary { background: #1f5fbf; color: #fff; border-color: #1f5fbf; }
    .rsp button.danger { background: #d64545; color: #fff; border-color: #d64545; }
    .rsp .stage { position: relative; display: inline-block; max-width: 100%; margin-top: 8px; background: #0f1620; border-radius: 8px; overflow: hidden; }
    .rsp .stage img { display: block; max-width: 100%; width: 848px; height: auto; cursor: crosshair; }
    .rsp .mark { position: absolute; width: 22px; height: 22px; margin: -11px 0 0 -11px; border: 2px solid #ffd23f; border-radius: 50%; box-shadow: 0 0 0 2px rgba(0,0,0,.5); pointer-events: none; display: none; }
    .rsp .mark::after { content: ""; position: absolute; left: 9px; top: 9px; width: 4px; height: 4px; background: #ffd23f; border-radius: 50%; }
    .rsp .hover { position: absolute; left: 8px; bottom: 8px; padding: 3px 8px; background: rgba(15,22,32,.8); color: #e8eef6; border-radius: 4px; font-size: 12px; pointer-events: none; }
    .rsp .status { margin-top: 8px; padding: 8px 10px; border-radius: 6px; background: #eef2f7; white-space: pre-wrap; }
    .rsp .status.warn { background: #fff4d6; } .rsp .status.err { background: #fde2e2; } .rsp .status.ok { background: #e3f5ea; }
    .rsp .target { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
    .rsp small { color: #5b6b7d; }
  `;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const fmt = (v, d = 3) => (typeof v === "number" ? v.toFixed(d) : "?");
  const fmtVec = (v, d = 3) => (Array.isArray(v) ? v.map((x) => fmt(x, d)).join(", ") : "?");

  // First value of an rxjs-style Observable (robotPositionService.getJointPositions()).
  function firstValue(observable) {
    return new Promise((resolve, reject) => {
      if (!observable || typeof observable.subscribe !== "function") {
        reject(new Error("no joint-position stream from PolyScope"));
        return;
      }
      let done = false;
      const sub = observable.subscribe({
        next: (v) => {
          if (done) return;
          done = true;
          try { sub && sub.unsubscribe && sub.unsubscribe(); } catch (e) { /* already closed */ }
          resolve(v);
        },
        error: reject,
        complete: () => { if (!done) reject(new Error("joint-position stream ended without a value")); },
      });
    });
  }

  // -- pose math (UR pose = [x, y, z, rx, ry, rz], rotation vector; 4x4 row-major) ----------
  function poseToMat(p) {
    const th = Math.hypot(p[3], p[4], p[5]);
    const [x, y, z] = th < 1e-12 ? [0, 0, 1] : [p[3] / th, p[4] / th, p[5] / th];
    const c = Math.cos(th), s = Math.sin(th), v = 1 - c;
    return [
      [c + x * x * v, x * y * v - z * s, x * z * v + y * s, p[0]],
      [y * x * v + z * s, c + y * y * v, y * z * v - x * s, p[1]],
      [z * x * v - y * s, z * y * v + x * s, c + z * z * v, p[2]],
      [0, 0, 0, 1],
    ];
  }
  function matToPose(m) {
    const cos = Math.min(1, Math.max(-1, (m[0][0] + m[1][1] + m[2][2] - 1) / 2));
    const th = Math.acos(cos);
    let r = [0, 0, 0];
    if (th > 1e-9 && Math.PI - th > 1e-6) {
      const k = th / (2 * Math.sin(th));
      r = [(m[2][1] - m[1][2]) * k, (m[0][2] - m[2][0]) * k, (m[1][0] - m[0][1]) * k];
    } else if (th > 1e-9) {
      // ~180°: the axis from the diagonal, signed by the largest component
      const ax = [0, 1, 2].map((i) => Math.sqrt(Math.max(0, (m[i][i] + 1) / 2)));
      const i = ax.indexOf(Math.max(...ax));
      for (let j = 0; j < 3; j++) if (j !== i) ax[j] = Math.sign(m[i][j] + m[j][i] || 1) * ax[j];
      r = ax.map((a) => a * th);
    }
    return [m[0][3], m[1][3], m[2][3], ...r];
  }
  function matMul(a, b) {
    return a.map((row) => [0, 1, 2, 3].map((j) => row.reduce((acc, v, k) => acc + v * b[k][j], 0)));
  }
  function matInv(m) {
    const R = [0, 1, 2].map((i) => [0, 1, 2].map((j) => m[j][i]));
    const t = [0, 1, 2].map((i) => -(R[i][0] * m[0][3] + R[i][1] * m[1][3] + R[i][2] * m[2][3]));
    return [[...R[0], t[0]], [...R[1], t[1]], [...R[2], t[2]], [0, 0, 0, 1]];
  }
  // Flange pose at the given joint angles from PolyScope's own DH table
  // (robotPositionService.getKinematicInfo(): [{DHTheta, DHa, DHd, DHAlpha}] × 6).
  function flangeMat(dh, q) {
    let T = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]];
    dh.forEach((j, i) => {
      const th = q[i] + (j.DHTheta || 0), ct = Math.cos(th), st = Math.sin(th);
      const ca = Math.cos(j.DHAlpha), sa = Math.sin(j.DHAlpha);
      T = matMul(T, [[ct, -st * ca, st * sa, j.DHa * ct], [st, ct * ca, -ct * sa, j.DHa * st], [0, sa, ca, j.DHd], [0, 0, 0, 1]]);
    });
    return T;
  }
  const withTimeout = (p, ms, why) =>
    Promise.race([p, new Promise((_, reject) => setTimeout(() => reject(new Error(why)), ms))]);

  class RealSensePilot extends HTMLElement {
    // PolyScope's IK solves for *PolyScope's* active TCP, which is not the TCP the cockpit's
    // controller reported (a different robot while testing in the sim; a stale training
    // offset on a real one). So the cockpit's flange target is re-expressed in PolyScope's
    // TCP: offset = FK_flange(0)⁻¹ · TCP(0), both from PolyScope at the zero joint vector.
    static polyScopeTarget(flangeTarget, dh, tcpAtZeroPose) {
      const offset = matMul(matInv(flangeMat(dh, [0, 0, 0, 0, 0, 0])), poseToMat(tcpAtZeroPose));
      return matToPose(matMul(poseToMat(flangeTarget), offset));
    }

    constructor() {
      super();
      this._node = null;
      this._api = null;
      this._robotSettings = null;
      this._robotContext = null;
      this._built = false;
      this._seq = 0;
      this._polling = false;
      this._stopped = false;
      this._blobUrl = null;
      this._located = null;
      this._hoverTimer = null;
      this._hoverPending = null;
    }

    // -- properties PolyScope X sets -----------------------------------------------
    get applicationNode() { return this._node; }
    set applicationNode(value) { this._node = value; this.render(); }
    get applicationAPI() { return this._api; }
    set applicationAPI(value) { this._api = value; }
    get robotSettings() { return this._robotSettings; }
    set robotSettings(value) { this._robotSettings = value; }
    get robotContext() { return this._robotContext; }
    set robotContext(value) { this._robotContext = value; }

    connectedCallback() { this._stopped = false; this.render(); }
    disconnectedCallback() {
      this._stopped = true;
      if (this._blobUrl) { URL.revokeObjectURL(this._blobUrl); this._blobUrl = null; }
    }

    // -- cockpit --------------------------------------------------------------------
    cockpitUrl() {
      return RealSensePilot.cockpitBase(this._node && this._node.cockpitUrl, location);
    }

    // The saved field as an absolute base URL. Shorthand is completed against this page's
    // host — ":7621" / "7621" → http://<page host>:7621, "host[:port]" → http://host[:port],
    // a bare host gets :7621. Anything without a scheme would otherwise be fetched *relative
    // to PolyScope's own page*, and PolyScope's 404 read as "the cockpit is outdated".
    static cockpitBase(saved, loc) {
      const raw = (saved ? String(saved) : "").trim().replace(/\/+$/, "");
      const pageHost = `${loc.protocol}//${loc.hostname}`;
      if (!raw) return `${pageHost}:${DEFAULT_COCKPIT_PORT}`;
      const port = /^:?(\d{1,5})$/.exec(raw);
      if (port) return `${pageHost}:${port[1]}`;
      if (/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)) return raw;
      const withScheme = `http://${raw}`;
      try {
        const u = new URL(withScheme);
        if (!u.port && u.pathname === "/") return `${withScheme}:${DEFAULT_COCKPIT_PORT}`;
      } catch (e) { /* diagnose() names the bad value */ }
      return withScheme;
    }

    // "Failed to fetch" is all the browser says, whether the cockpit refused this page's
    // origin (CORS) or nothing answered. A no-cors request tells them apart: it resolves
    // (opaque) when the server is up, whatever its CORS list, and rejects when it isn't.
    async diagnose() {
      const base = this.cockpitUrl();
      const origin = location.origin;
      let target;
      try { target = new URL(base); } catch (e) {
        return `"${base}" is not a URL — enter it as http://<host>:7621`;
      }
      if (location.protocol === "https:" && target.protocol === "http:") {
        return `this page is https and the cockpit is http — the browser blocks that (mixed content).`;
      }
      const ctl = typeof AbortController === "function" ? new AbortController() : null;
      const timer = ctl ? setTimeout(() => ctl.abort(), PROBE_TIMEOUT_MS) : null;
      try {
        await fetch(`${base}/api/info`, { mode: "no-cors", cache: "no-store", signal: ctl ? ctl.signal : undefined });
        return (
          `the cockpit at ${base} is running but refuses this page (origin ${origin}).\n` +
          `Restart it with  --cors ${origin}  (or PERCEPTION_CORS=${origin}); ` +
          `a cockpit started before --cors existed needs a restart on the new code.`
        );
      } catch (e) {
        const timedOut = e && e.name === "AbortError";
        const hints = [];
        if (LOOPBACK.has(target.hostname) && !LOOPBACK.has(location.hostname)) {
          hints.push(`"${target.hostname}" is the machine running this browser, not necessarily the cockpit's — use the cockpit host's IP`);
        } else if (!LOOPBACK.has(target.hostname)) {
          hints.push("a cockpit listens on loopback only unless started with --bind 0.0.0.0");
        }
        hints.push(timedOut ? `no answer in ${PROBE_TIMEOUT_MS / 1000} s — firewall or wrong subnet?` : "wrong host or port, or the cockpit is not running");
        return `nothing answers at ${base} from this browser.\n` + hints.map((h) => `· ${h}`).join("\n");
      } finally {
        if (timer) clearTimeout(timer);
      }
    }

    async api(method, path, body) {
      const init = { method, headers: {} };
      if (body !== undefined) {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }
      const r = await fetch(this.cockpitUrl() + path, init);
      let out;
      try { out = await r.json(); } catch (e) { out = { ok: false, error: `HTTP ${r.status}` }; }
      if (out && out.ok === undefined) out.ok = r.ok;
      return out;
    }

    // -- rendering --------------------------------------------------------------------
    $(name) { return this.querySelector(`[data-rsp="${name}"]`); }

    render() {
      if (!this._node) return;
      if (!this._built) {
        this.innerHTML = `
          <style>${CSS}</style>
          <div class="rsp">
            <h2><span class="dot" data-rsp="dot"></span> RealSense Pilot <small data-rsp="fps"></small></h2>
            <div class="row">
              <label for="rsp-url">Cockpit</label>
              <input id="rsp-url" type="text" data-rsp="url" placeholder="http://<jetson-or-laptop>:7621 (empty = this host:7621)" />
              <button data-rsp="save">Save</button>
              <a data-rsp="open" href="#" target="_blank" rel="noopener">Open cockpit</a>
            </div>
            <div class="stage" data-rsp="stage">
              <img data-rsp="img" alt="wrist camera" draggable="false" />
              <div class="mark" data-rsp="mark"></div>
              <div class="hover" data-rsp="hover">hover for depth · click a point</div>
            </div>
            <div class="status" data-rsp="status">connecting…</div>
            <div class="target" data-rsp="target"></div>
            <div class="row">
              <button class="primary" data-rsp="move-ps" disabled title="Inverse kinematics + PolyScope's auto-move screen (hold to move)">Move (PolyScope)</button>
              <button data-rsp="move-ck" disabled title="The cockpit moves the arm over Primary through urctl's safety envelope">Move (cockpit)</button>
              <button data-rsp="bringup" title="power on + brake release + unlock protective stop, via the cockpit">Bring up</button>
              <button class="danger" data-rsp="stop">STOP</button>
              <button data-rsp="clear">Clear</button>
            </div>
            <small>Click = segment at the pixel → point in the base frame through the hand-eye → approach pose above it. Reach is checked before a move is offered.</small>
          </div>`;
        this.wire();
        this._built = true;
        this.startPolling();
      }
      this.$("url").value = this._node.cockpitUrl || "";
      this.$("open").href = this.cockpitUrl() + "/";
    }

    wire() {
      const img = this.$("img");
      img.addEventListener("click", (ev) => this.onClick(ev));
      img.addEventListener("mousemove", (ev) => this.onHover(ev));
      img.addEventListener("mouseleave", () => { this.$("hover").textContent = "hover for depth · click a point"; });
      this.$("save").addEventListener("click", () => this.saveCockpitUrl());
      this.$("url").addEventListener("keydown", (ev) => { if (ev.key === "Enter") this.saveCockpitUrl(); });
      this.$("move-ps").addEventListener("click", () => this.movePolyScope());
      this.$("move-ck").addEventListener("click", () => this.moveCockpit());
      this.$("bringup").addEventListener("click", () => this.bringUp());
      this.$("stop").addEventListener("click", () => this.stop());
      this.$("clear").addEventListener("click", () => this.clearTarget());
    }

    setStatus(text, kind) {
      const el = this.$("status");
      if (!el) return;
      el.textContent = text;
      el.className = "status" + (kind ? ` ${kind}` : "");
    }

    setLive(live, fps) {
      const dot = this.$("dot");
      if (dot) dot.className = "dot " + (live ? "live" : "dead");
      const f = this.$("fps");
      if (f) f.textContent = live && fps ? `${Number(fps).toFixed(1)} fps` : "";
    }

    async saveCockpitUrl() {
      const value = this.$("url").value.trim();
      this._node.cockpitUrl = value;
      this._seq = 0;
      this.$("open").href = this.cockpitUrl() + "/";
      try {
        if (this._api && this._api.applicationNodeService) {
          await this._api.applicationNodeService.updateNode(this._node);
        }
        this.setStatus(`cockpit: ${this.cockpitUrl()}`);
      } catch (err) {
        this.setStatus(`could not save the node: ${err && err.message ? err.message : err}`, "err");
      }
    }

    // -- the feed -----------------------------------------------------------------------
    async startPolling() {
      if (this._polling) return;
      this._polling = true;
      const img = this.$("img");
      let announced = false;
      while (!this._stopped && this.isConnected) {
        try {
          const r = await fetch(`${this.cockpitUrl()}/api/color.png?after=${this._seq}&timeout_ms=${POLL_TIMEOUT_MS}`);
          if (r.status === 503) {
            this.setLive(false);
            this.setStatus("the cockpit is up but has no frame yet (camera opening?)", "warn");
            await sleep(500);
            continue;
          }
          if (!r.ok) {
            const isPolyScope = new URL(this.cockpitUrl()).origin === location.origin;
            const e = new Error(isPolyScope
              ? `${this.cockpitUrl()} is PolyScope itself, not the cockpit — the cockpit listens on port ${DEFAULT_COCKPIT_PORT}.`
              : `the cockpit at ${this.cockpitUrl()} answered HTTP ${r.status} on /api/color.png` +
                (r.status === 404 ? " — it predates the URCap routes; update it and restart." : "."));
            e.http = r.status;
            throw e;
          }
          const seq = Number(r.headers.get("X-Seq") || 0);
          const blob = await r.blob();
          const url = URL.createObjectURL(blob);
          const previous = this._blobUrl;
          img.onload = () => { if (previous) URL.revokeObjectURL(previous); };
          img.src = url;
          this._blobUrl = url;
          if (seq) this._seq = seq;
          this.setLive(true, r.headers.get("X-Fps"));
          if (!announced) { this.setStatus(`live from ${this.cockpitUrl()}`, "ok"); announced = true; }
        } catch (err) {
          announced = false;
          this.setLive(false);
          const why = err && err.message ? err.message : String(err);
          // Only a network-level failure is ambiguous; an HTTP status already says what happened.
          let detail = err && err.http ? why : null;
          if (!detail) { try { detail = await this.diagnose(); } catch (e) { detail = null; } }
          const up = detail && detail.includes("is running but");
          this.setStatus(
            (detail || `no cockpit at ${this.cockpitUrl()} (${why}).`) +
            (up ? "" : `\nStart one where the camera is:  perception --cell <cell> gui --cors ${location.origin}` +
              `\nthen set its URL above.`),
            "err",
          );
          await sleep(1500);
        }
      }
      this._polling = false;
    }

    // -- pixels -----------------------------------------------------------------------
    pixelOf(ev) {
      const img = this.$("img");
      const rect = img.getBoundingClientRect();
      if (!img.naturalWidth || !rect.width) return null;
      const x = Math.round((ev.clientX - rect.left) * (img.naturalWidth / rect.width));
      const y = Math.round((ev.clientY - rect.top) * (img.naturalHeight / rect.height));
      return {
        x: Math.max(0, Math.min(img.naturalWidth - 1, x)),
        y: Math.max(0, Math.min(img.naturalHeight - 1, y)),
        fx: (ev.clientX - rect.left) / rect.width,
        fy: (ev.clientY - rect.top) / rect.height,
      };
    }

    onHover(ev) {
      const px = this.pixelOf(ev);
      if (!px) return;
      this._hoverPending = px;
      if (this._hoverTimer) return;
      this._hoverTimer = setTimeout(async () => {
        this._hoverTimer = null;
        const p = this._hoverPending;
        try {
          const res = await this.api("GET", `/api/point?x=${p.x}&y=${p.y}`);
          const el = this.$("hover");
          if (!el) return;
          el.textContent = res.ok
            ? `(${p.x}, ${p.y}) → ${fmtVec(res.point_m)} m in the camera · ${fmt(res.point_m[2], 2)} m away`
            : `(${p.x}, ${p.y}) → no depth`;
        } catch (e) { /* the feed loop reports connectivity */ }
      }, HOVER_MS);
    }

    async onClick(ev) {
      const px = this.pixelOf(ev);
      if (!px) return;
      const mark = this.$("mark");
      mark.style.left = `${px.fx * 100}%`;
      mark.style.top = `${px.fy * 100}%`;
      mark.style.display = "block";
      this._located = null;
      this.$("move-ps").disabled = true;
      this.$("move-ck").disabled = true;
      this.setStatus(`segmenting at (${px.x}, ${px.y})…`);
      try {
        const seg = await this.api("POST", "/api/segment", { x: px.x, y: px.y });
        if (!seg.ok) throw new Error(seg.error || "segment failed");
        const point = seg.features && seg.features.point_m;
        if (!point) throw new Error("the segment has no depth point — click on the object, not the void");
        const loc = await this.api("POST", "/api/robot/locate", { point_m: point });
        if (!loc.ok) throw new Error(loc.error || "locate failed (is the cockpit's robot link up?)");
        this._located = loc;
        const reach = loc.reachable === false ? "OUT OF REACH" : loc.reachable === true ? "reachable" : "reach unknown";
        this.$("target").textContent =
          `object  base ${fmtVec(loc.point_base_m)} m  (${fmt(loc.point_distance_m, 2)} m from the base)\n` +
          (loc.reference === "flange" && loc.flange_target_pose
            ? `flange   ${fmtVec(loc.flange_target_pose)}  standoff ${fmt(loc.standoff_m, 2)} m above the object\n`
            : `approach ${fmtVec(loc.approach_pose)}  standoff ${fmt(loc.standoff_m, 2)} m\n`) +
          `${reach}` +
          (loc.reach_check === "controller_ik"
            ? "  (the controller's inverse kinematics)"
            : loc.max_reach_m ? `  (${fmt(loc.max_reach_m, 2)} m datasheet radius${loc.model ? `, ${loc.model}` : ""} — no IK answer)` : "");
        const offer = loc.reachable !== false;
        this.$("move-ps").disabled = !offer;
        this.$("move-ck").disabled = !offer;
        this.setStatus(offer ? "target located — choose how to move" : "target located but out of reach — pick a nearer point", offer ? "ok" : "warn");
      } catch (err) {
        this.setStatus(`${err && err.message ? err.message : err}`, "err");
      }
    }

    clearTarget() {
      this._located = null;
      this.$("mark").style.display = "none";
      this.$("target").textContent = "";
      this.$("move-ps").disabled = true;
      this.$("move-ck").disabled = true;
      this.api("POST", "/api/clear").catch(() => {});
      this.setStatus("cleared");
    }

    // -- moving ------------------------------------------------------------------------
    async movePolyScope() {
      const loc = this._located;
      if (!loc) return;
      const api = this._api;
      if (!api || !api.robotPositionService || !api.robotMoveService) {
        this.setStatus("PolyScope's move services are not on this API — use Move (cockpit)", "warn");
        return;
      }
      const rps = api.robotPositionService;
      try {
        this.setStatus("asking PolyScope for the joint solution…");
        const qNear = await firstValue(rps.getJointPositions());
        let pose = loc.approach_pose;
        if (Array.isArray(loc.flange_target_pose) && typeof rps.getKinematicInfo === "function") {
          const dh = await rps.getKinematicInfo();
          const zero = { base: 0, shoulder: 0, elbow: 0, wrist1: 0, wrist2: 0, wrist3: 0 };
          const t0 = await rps.convertJointPositionsToTcpPose(zero);
          pose = RealSensePilot.polyScopeTarget(loc.flange_target_pose, dh, [...t0.position, ...t0.orientation]);
        }
        // PolyScope's IK does not reject an unsolvable pose, it never answers (10.13 sim).
        const joints = await withTimeout(
          rps.getInverseKinematics(
            { position: [pose[0], pose[1], pose[2]], orientation: [pose[3], pose[4], pose[5]] },
            qNear,
          ),
          8000,
          `PolyScope found no joint solution for [${fmtVec(pose)}] in 8 s — unreachable for its IK`,
        );
        // autoMove resolves as soon as PolyScope's screen opens (10.13 sim), not when the arm arrives
        await api.robotMoveService.autoMove(joints);
        this.setStatus("PolyScope's move screen is open — hold Move To Position to go there", "ok");
      } catch (err) {
        this.setStatus(`PolyScope move: ${err && err.message ? err.message : err}`, "err");
      }
    }

    async moveCockpit() {
      const loc = this._located;
      if (!loc) return;
      this.$("move-ck").disabled = true;
      this.setStatus("moving through the cockpit (Primary, safety-enveloped)…");
      try {
        const res = await this.api("POST", "/api/robot/move", { pose: loc.approach_pose, velocity: 0.1 });
        if (res.ok) {
          this.setStatus(`landed at ${fmtVec(res.landed)}`, "ok");
        } else {
          const why = res.error || (res.safety && res.safety.violations && res.safety.violations.join("; ")) ||
            (res.protective_stop ? "protective stop — Bring up, then retry" : "the move did not confirm");
          this.setStatus(`move refused: ${why}`, "err");
        }
      } catch (err) {
        this.setStatus(`move: ${err && err.message ? err.message : err}`, "err");
      } finally {
        this.$("move-ck").disabled = !this._located;
      }
    }

    async bringUp() {
      this.setStatus("bringing the robot up…");
      try {
        const res = await this.api("POST", "/api/robot/bring_up");
        this.setStatus(res.ok ? `robot ${res.robot_mode || "up"} / ${res.safety_mode || ""}` : `bring-up failed: ${res.error || "?"}`, res.ok ? "ok" : "err");
      } catch (err) {
        this.setStatus(`bring-up: ${err && err.message ? err.message : err}`, "err");
      }
    }

    async stop() {
      try {
        const res = await this.api("POST", "/api/robot/stop");
        this.setStatus(res.ok ? "stopped" : `stop failed: ${res.error || "?"}`, res.ok ? "warn" : "err");
      } catch (err) {
        this.setStatus(`stop: ${err && err.message ? err.message : err}`, "err");
      }
    }
  }

  if (!window.customElements.get(TAG)) {
    window.customElements.define(TAG, RealSensePilot);
  }
})();
