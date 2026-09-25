# TODO

## Open questions for Nick

- 2026-09-02 — `Dockerfile.perception` pins librealsense at a tag verified to exist; the on-controller GPU target is **not decided** (UR AI Accelerator as a URCap-X container vs a separate Jetson). When it is: which JetPack / L4T base to pin for CUDA + SAM.
- 2026-09-12 — Controller IPs for the `ur3` (UR3e, PolyScope 5) and `ur20` (PolyScope X) cells — `perception/cells/*.env` ship with `UR_HOST` empty.
- 2026-09-12 — The Windows path (`scripts/setup-windows.ps1`, `scripts/cockpit.ps1`, `REALSENSE_LIB` at the SDK's default `bin\x64\realsense2.dll`) is written against the librealsense repo's own references but has NOT been run on a Windows box yet. First run on the work laptop is the verification; paste the doctor output (`uv run perception --cell ur20 doctor --stream --json`) if anything fails.

- 2026-09-25 — `perception pick-cycle` needs a minimum-radius skip (or a via-point) for blocks within ~0.25 m of the UR3e's column: the hover approach to the block at radius 0.24 m protective-stopped in both passes.
- 2026-09-25 — Hand-E picks work on the UR3e (orbit calibration RMS 4.4 mm, one pick+place done). Open: the running cockpit predates the gripper route, so `pick` used `urctl gripper` from a second process — restart the cockpit to get `POST /api/robot/gripper` and the Open/Close buttons; the orbit calibration, pick, place and timelapse scripts live in `scripts/pilot/` (scratch, `# ruff: noqa`), not yet `perception` commands; `place.py` should check the drop spot's clearance from the rail (the second show place protective-stopped at the rail corner); the finger opening axis vs flange X is unverified (both yaws gripped).
- 2026-09-25 — The Mac Studio lost the D435 claim race with nothing else on the bus (43 libusb capture-resets in 40 s, `docs/realsense.md` §Troubleshooting). Drop the Mac as a camera host (laptop/WSL2 + Jetson only), or spend one more flagged experiment on it: fewer handle opens per start. **Built 2026-09-25** as `perception gui --rs-lean` / `PERCEPTION_RS_LEAN=1` (no USB-type probe, no mode enumeration, no preset/laser writes, `RS2_OPTION_GLOBAL_TIME_ENABLED` off on every sensor right after `pipeline_start` — the pipeline's own device has no earlier hook). **Verified 2026-09-25**: with `--rs-lean` the cockpit streams on the Mac (first open errors once, the back-off re-open holds). Open: why the *first* open still loses — one more reset to shave, or accept the one retry. Already ruled out: software drift, headless-vs-desktop, a fresh daemon, the webcams, Spotify.

## Decisions (so they don't get re-asked)

- 2026-09-04 — Perception / send-to-robot work targets PolyScope X; the e-Series sim on the Mac Studio is abandoned (Xvfb dies under Rosetta, URControl under QEMU). URSim e-Series stays CI-only.
- 2026-09-12 — The UR3e in the test cell is **PolyScope 5 (e-Series)**; the UR20 is PolyScope X. Cells carry the platform, so both work; `UR_PLATFORM` keeps its `e-series` default (no global flip).
- 2026-09-12 — Bracket camera side: +Y, the tool-I/O side (Rev B); the UR20 print clocked 45° off its M8 socket.
- 2026-09-12 — Hand-eye calibration routine in the cockpit: **yes, build it** before the demo.
- 2026-09-12 — Repo push: fork `Olympus-Controls/UR-utils` and push the branch to the fork; PR from there.
- 2026-09-12 — D435 USB-C is on an **end face** of Nick's unit (not the back as in Intel's mesh); bracket clears it, spec §6 A1 updated.
- 2026-09-12 — SAM backend stays "wired, unverified" until a GPU box exists; the demo uses the stub segmenter.
- 2026-09-12 — Dependabot ignores `universalrobots/ursim_polyscopex` bumps (pinned 10.13.0; re-test new tags by hand).

## Monocular scan — backburner (2026-09-24, `docs/mono-scan.md`)

Built and verified on the synthetic rig; parked until there is time at the UR3e. Everything below needs hardware, a print, or a purchase — the software side is done up to the point marked.

**Needs the 3D prints (`hardware/charuco-board/`):**
- Print the ChArUco plate + the touch probe (flat seating face, no pilot); print the pattern PDF at 100 %, cut on the line, A corner at the notch.
- `perception --cell ur3 touch --set plate --label A|B|C` with the probe (TCP `[0,0,0.050]` on the bracket) → `T_base_plate`.
- `perception latency-fit <sweep over the plate> --save` — the ChArUco path (PnP per frame). Until then the print-free `--objects` path (fit the latency by silhouette IoU over the blocks) is the way.
- ChArUco hand-eye command (`perception/charuco.py` has the closed-form per-view solve + averaging; no CLI yet): needed for any camera that is *not* the RealSense — the current hand-eye came from the depth-based touch-and-click routine.
- Camera-intrinsics command for a non-RealSense camera (ChArUco calibration; the RealSense reports its own).

**Needs the UR3e (no print):**
- `perception --cell ur3 table-from-depth` (one RGB-D frame of the empty surface → table plane) — or three flange-centre touches with `touch --probe-tcp 0 0 0 0 0 0`.
- First sweep at 0.05 m/s (rolling-shutter colour): `perception --cell ur3 scan --parts parts/block_50x30x30.stl`; read `depth_check` per object (the RealSense depth grades the mono result).
- `perception latency-fit <that sweep> --objects --save`, then raise the sweep speed.
- Try `--stream ir` (left IR imager = global shutter, emitter off): built blind against the SDK's C API, **unverified on the camera**; verify the IR frame arrives and the hand-eye (depth frame) applies without extrinsics.
- Cockpit **Scan** panel + `cam_scan*` MCP tools on the real cell (verified on the synthetic cockpit only).

**Later / purchases:**
- Global-shutter camera with a trigger input (Arducam OV9281/OV9782 USB, or IMX296 CSI on the Thor) + a 24 V→3.3 V level shifter: the T2 trigger (UR tool DO → camera, edges read from RTDE's `actual_digital_output_bits`; `PoseRecorder.edges()` already extracts them).
- Line laser (650 nm module + bandpass filter) on the bracket for textureless/reflective parts; laser-plane calibration over the plate.
- Model-based edge matching for non-box parts (the box fit uses the OBB corners; use the CAD hull's silhouette).
- Stable-pose analysis from the convex hull (resting poses are OBB-based now: exact for boxes only).
- NVIDIA Thor: CUDA SGBM / SAM; `Dockerfile.perception` retarget.

## Open questions for Nick — pruning plan (2026-09-25)

- Prune scope A (delete): the 2-D `perceive` pipeline (pipeline/tools/depth/blobs/sources/backends blob_cv+depth_anything, CLI synthetic/capture/image/call, `depth` extra), the monocular-scan experiment (scan_cli, monocam, posestream, sweep, locate2d, boxfit, tableplane, depthcheck, touch, charuco, latency, partlib, charuco-board hardware, parts/, mono-scan.md, cockpit Scan panel, cam_scan*/cell_table_from_depth), dead bits (urctl/urp.py, RealSenseSource, programs/Showcase, docs story/index/dossier/console png, empty `mcp` extra, rs_probe.sh, pilot wiggle/orbit_cal*.py once folded in). ~9k of 19k lines. OK to go?
- Guided/inspect wizard + LiveReloader placers + InspectionBot/Dance/AppleStack/PickPlace programs + ur-pick-from-image skill: delete, or keep as an optional UR-only module? (urp_convert + urp_builder + NodeTreeDemo + MotionDemo stay: loading programs is core control.)
- One GUI: retire `urctl-gui` (urctl/webapp.py + its index.html) in favour of the cockpit?
- sysinfo/installation + `ur_system_snapshot`/`ur_list_programs` (SSH/docker filesystem introspection): delete? The UR3e has no SSH; RTDE deep state + codes stay.
- CaptureStore (RealSenseTrainer dataset captures: `rs-capture`, /api/capture, cam_capture): delete, leaving /api/snapshot?
- Demo view (`--demo`) and docs/realsense-cell.html: keep through the demo, prune after?
- Restructure: introduce a `Controller` interface (state/bring_up/move_joints/move_tcp/path/stop/freedrive/program/native_script) with the UR implementation first, a `Gripper` interface (Robotiq-over-URCap first), and drop the `ur_` prefix from tool names (with aliases)? This is the precondition for Fanuc and for keeping PolyScope X a config switch.
- SAM extra: keep the hook for a GPU box, or drop until then?
