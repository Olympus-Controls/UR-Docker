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

  class RealSensePilot extends HTMLElement {
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
      const saved = (this._node && this._node.cockpitUrl ? String(this._node.cockpitUrl) : "").trim();
      if (saved) return saved.replace(/\/+$/, "");
      return `${location.protocol}//${location.hostname}:${DEFAULT_COCKPIT_PORT}`;
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
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
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
          this.setStatus(
            `no cockpit at ${this.cockpitUrl()} (${err && err.message ? err.message : err}).\n` +
            `Start one where the camera is:  perception --cell <cell> gui --cors ${location.origin}\n` +
            `then set its URL above.`,
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
          `approach ${fmtVec(loc.approach_pose)}  standoff ${fmt(loc.standoff_m, 2)} m  ${reach}` +
          (loc.max_reach_m ? `  (cap ${fmt(loc.max_reach_m, 2)} m${loc.model ? `, ${loc.model}` : ""})` : "");
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
      const pose = loc.approach_pose;
      try {
        this.setStatus("asking PolyScope for the joint solution…");
        const qNear = await firstValue(api.robotPositionService.getJointPositions());
        const joints = await api.robotPositionService.getInverseKinematics(
          { position: [pose[0], pose[1], pose[2]], orientation: [pose[3], pose[4], pose[5]] },
          qNear,
        );
        this.setStatus("opening PolyScope's auto-move screen — hold to move", "ok");
        await api.robotMoveService.autoMove(joints);
        this.setStatus("auto-move finished", "ok");
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
