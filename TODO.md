# TODO

## Needs the cell (UR3e + Hand-E; demo Monday 2026-09-28)

- 2026-09-25 — Four pick-cycle faults could not be injected from the desk and are untested claims until someone does them once: pendant flipped to **Local** mid-run, a webcam or the D435 **unplugged** mid-run, the cockpit **restarted** while the routine is on a block, robot **power cut**. Note what the routine did in the field log.
- 2026-09-25 — The survey sees 3 of 4 blocks from the three overlook poses: add a `--survey-pose` from the far side of the pile. The second look sometimes shifts a centre 20–30 mm and closes on nothing — 0.4 m looks like the edge of the D435's depth on foam; try surveying from 0.3 m.
- 2026-09-25 — `pick-cycle --min-radius-m` is opt-in (default 0). On the UR3e pass `0.25` (the block at 0.24 m protective-stopped twice), or teach a via-point. A per-model default would remove the flag.
- 2026-09-25 — Mac Studio + D435: `--rs-lean` streams (first open errors once, the back-off re-open holds). Open: why the *first* open still loses — shave one more reset, or accept the one retry. Ruled out: software drift, headless-vs-desktop, a fresh daemon, the webcams, Spotify. The Mac is a dev box, not a camera host; Windows laptop (WSL2, verified 09-23) and Jetson are.
- 2026-09-12 — The native Windows path (`scripts/setup-windows.ps1`, `scripts/cockpit.ps1`, `REALSENSE_LIB` at the SDK's default `bin\x64\realsense2.dll`) has not been run on a Windows box. First run on the work laptop is the verification; paste the doctor output (`uv run perception --cell ur20 doctor --stream --json`) if anything fails.

## PolyScope X URCap (2026-09-26, `docs/polyscopex-urcap.md`)

- **Install into the sim** — blocked on Remote mode: flip it on the sim's Safety screen (password `operator`), then `make urcap-install` (package is built: `target/realsense-pilot-0.1.0.urcapx`); the fake cockpit for it is `make urcap-cockpit` (:7622, CORS for :8000). Then: does the node appear under Application, does the feed show, does a click locate, does IK + auto-move accept the pose (is `Pose.orientation` a rotation vector?).
- Real camera in the URCap: restart the live cockpit with `--cors http://localhost:8000` (your `Quickstart` line + that flag) and set the node's cockpit URL to `http://localhost:7621`.
- Later, on the robot: a backend-container packaging of the cockpit (`containers:` + `devices: video` + `services: urcontrol-primary`) so the pendant needs no external host.

## Code (no robot needed)

- 2026-09-26 — `perception calibrate` (orbit hand-eye) is built and tested on the fake cell; **run it once on the UR3e** (`--dry-run` first), then delete `scripts/pilot/orbit_cal*.py` + `wiggle.py`. `record.py`/`show.py`/`assemble.py` stay as the timelapse tooling. `place.py`'s lesson carries: a place spot needs the same clearance check as a pick.

## Open questions for Nick

- 2026-09-02 — The on-controller GPU is a **Jetson** (decided 2026-09-25); which JetPack / L4T base to pin in `Dockerfile.perception` for CUDA + SAM is still open.
- 2026-09-25 — Pruning, second half. Scope A was only half done: the monocular scanner and the dead bits are gone, but the pre-RealSense **2-D `perceive` pipeline** is still here (`pipeline`/`depth`/`blobs`/`sources`/`tools`/`factory`, backends `blob_cv` + `depth_anything`, CLI `synthetic`/`capture`/`image`/`call`/`tools`, the `depth` extra, `docs/perception.md`, ~2k lines; its `perception/tools.py` registry is merged into `urctl-mcp` and the `urctl` GUI). Delete?
- 2026-09-25 — Guided/inspect wizard (`urctl/guided.py`, `LiveReloader` placers) + `InspectionBot`/`Dance`/`AppleStack`/`PickPlace` programs + the `ur-pick-from-image` skill: delete, or keep as an optional UR-only module? (`urp_convert` + `urp_builder` + `NodeTreeDemo` + `MotionDemo` stay: loading programs is core control.)
- 2026-09-25 — One GUI: retire `urctl-gui` (`urctl/webapp.py` + its `index.html`) in favour of the cockpit?
- 2026-09-25 — `sysinfo`/`installation` + `ur_system_snapshot`/`ur_list_programs` (SSH/docker filesystem introspection): delete? Neither the UR3e nor PolyScope X exposes SSH; RTDE deep state + `codes` stay.
- 2026-09-25 — Restructure: a `Controller` interface (state/bring_up/move_joints/move_tcp/path/stop/freedrive/program/native_script) with the UR implementation first, a `Gripper` interface (Robotiq-over-URCap first), and the `ur_` prefix dropped from tool names (with aliases)? Precondition for Fanuc and for keeping PolyScope X a config switch. After the demo?
- 2026-09-25 — SAM extra: keep the hook for the Jetson, or drop until it exists?

## Decisions (so they don't get re-asked)

- 2026-09-25 — **Final deployment: a Jetson next to a UR running PolyScope X**, with room for a second Jetson on other arms. Controller IPs are always different — cells carry them (`perception/cells/*.env`); `ur20.env` is filled on site, not in the repo.
- 2026-09-25 — **Monday 2026-09-28 demo is on the e-Series UR3e.** The platform must stay compatible for ~10 years; the demo view (`--demo`) and `docs/realsense-cell.html` stay through the demo.
- 2026-09-25 — Prune mandate: drop what is not inherent to gross vision guidance + robust agentic control; keep it portable (Windows, Mac, Jetson, Linux, new cameras) and open to other arms/controllers (Fanuc, PolyScope X).
- 2026-09-25 — **Monocular scanner dropped** ("don't worry about it for now"). Removed in `200d175`; the parent commit has it if the idea comes back. The ChArUco plate/probe prints, the `depth` extra's backend, the trigger-camera and line-laser purchases went with it.
- 2026-09-25 — Pick-cycle geometry (verified in person): approach along the **tool** axis; the Hand-E fingers travel along flange **Y**; a yaw about the down-pointing tool Z is the negative of the base-heading yaw. Each block is one compiled URScript program; the cockpit does vision only.
- 2026-09-25 — Hand-E: the Robotiq URCap daemon is loopback-only on the controller, so the gripper is driven from URScript (`urctl gripper`, `ur_gripper`, `POST /api/robot/gripper`).
- 2026-09-25 — Mac as D435 host: no (43 libusb resets per start; `docs/realsense.md` §Troubleshooting). `--rs-lean` is the flagged exception for dev.
- 2026-09-04 — Perception / send-to-robot work targets PolyScope X; the e-Series sim on the Mac Studio is abandoned (Xvfb dies under Rosetta, URControl under QEMU). URSim e-Series stays CI-only.
- 2026-09-12 — The UR3e in the test cell is **PolyScope 5 (e-Series)**; the UR20 is PolyScope X. Cells carry the platform, so both work; `UR_PLATFORM` keeps its `e-series` default (no global flip).
- 2026-09-12 — Bracket camera side: +Y, the tool-I/O side (Rev B); the UR20 print clocked 45° off its M8 socket.
- 2026-09-12 — Hand-eye calibration routine in the cockpit: **yes, build it** before the demo. (Built: touch-and-click 09-23, orbit 09-25.)
- 2026-09-12 — Repo push: fork `Olympus-Controls/UR-utils` and push the branch to the fork; PR from there.
- 2026-09-12 — D435 USB-C is on an **end face** of Nick's unit (not the back as in Intel's mesh); bracket clears it, spec §6 A1 updated.
- 2026-09-12 — SAM backend stays "wired, unverified" until a GPU box exists; the demo uses the stub segmenter.
- 2026-09-12 — Dependabot ignores `universalrobots/ursim_polyscopex` bumps (pinned 10.13.0; re-test new tags by hand).
