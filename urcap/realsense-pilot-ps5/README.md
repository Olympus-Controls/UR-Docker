# RealSense Pilot for PolyScope 5 (e-Series)

The same operator tool as the PolyScope X node in `../realsense-pilot/`, for an e-Series
robot on PolyScope 5: the wrist RealSense feed inside PolyScope, tap a point, and send
the arm there. It is an **Installation node** (Installation tab → URCaps → RealSense
Pilot), the PolyScope 5 counterpart of PolyScope X's Application node.

Download: [`../dist/realsense-pilot-ps5-0.4.0.urcap`](../dist/realsense-pilot-ps5-0.4.0.urcap)

## Install on the robot

1. Copy `realsense-pilot-ps5-0.4.0.urcap` to a USB stick and plug it into the pendant.
2. Settings (☰ top right) → System → URCaps → **+** → pick the file → Open, then
   **Restart** when PolyScope asks.
3. Installation tab → URCaps → **RealSense Pilot**.

## The cockpit it talks to

The node is a client of the RealSense cockpit (`perception gui`) running on the computer
the camera is plugged into. The controller reaches it over the cell network, so start it
listening on the network:

    perception --cell ur3 gui --bind 0.0.0.0

and type `http://<that-computer's-ip>:7621` into **Cockpit** (the pendant keyboard opens
when you tap the field), then **Save** — it is kept in the installation. No `--cors` is
needed: the node is Java on the controller, not a web page.

## What is the same as PolyScope X, and what is not

| PolyScope X node | PolyScope 5 node |
| --- | --- |
| live dot, fps, Cockpit field + Save, feed, status, located target | same |
| hover for depth, click to segment → locate | same (hover works with a mouse; on the touchscreen, tap) |
| **Move (PolyScope)**: PolyScope's IK + auto-move screen | PolyScope's hold-to-move screen, sent the **controller's own joint solution** (`joint_target` from locate: `get_inverse_kin` with the TCP forced to the flange) — no IK through an active TCP the cockpit may disagree about |
| Move (cockpit), Bring up, STOP, Clear | same |
| Open cockpit (a browser tab) | removed — the pendant has no browser |

Reach is judged by the controller's inverse kinematics (`get_inverse_kin_has_solution`)
when the cockpit can ask it, and by the datasheet radius only when it cannot; the target
text says which.

## Releases

Pushing a `urcap5-v<version>` tag (`git tag urcap5-v0.3.0 && git push fork urcap5-v0.3.0`)
publishes the committed `../dist/realsense-pilot-ps5-<version>.urcap` and its sha256 as a
GitHub Release (`.github/workflows/release-urcap5.yml`). The workflow does not rebuild the
jar — the URCap API jars are UR's and live only in the URSim image — it runs
`python3 urcap/urcap5.py release-check <tag>`: the tag's version is `Bundle-Version`, the
committed jar of that version exists, carries the current sources' digest, and passes
PolyScope 5's install checks; then the URCap tests. So: bump `Bundle-Version`, `make
urcap5-package`, commit `dist/`, then tag. The Python package's `v*` tags are a separate line.
## RealSense Pick: tell it the part's size

The **RealSense Pick** program node (Program tab → URCaps) finds white blocks by colour and
depth. Without a part size it takes anything block-sized (the foam blocks' fixed gate:
at most 70 × 60 mm). Give it the part's rough size **as it lies on the table** — **Length**
and **Width** (the footprint, either way round), optionally **Height** (how far its top
stands above the table), and a **Tolerance** (default ±25 %, never tighter than ±5 mm) —
and it considers only candidates that size, both on the teach screen and when the program
runs:

- the feed rings the matching parts green and the rest grey and dashed, labelled with what
  they measured and why they are out (`43×43 mm too short`, `2 parts touching?`, `too flat`);
- tapping a ruled-out one says so; the program picks the matching part nearest the tap;
- the height tells a block from a flat look-alike of the same footprint (a sticker, a
  label, a sheet) and is measured from the surface around the part — when no surface is
  visible there the height is simply not checked;
- with a height set, a grip depth that would put the fingertips on the table is refused
  before the program can run;
- a run that sees white things but none the part's size stops with its own reason
  (status −7, "nothing the size of the part"), not "no block in view".

On the wire the node adds `part=60x40x30 tol=25` to its `FIND` / `REFINE` lines
(`perception/partspec.py`; the cockpit's `/api/pick/detect?part=…&tol=…` and
`/api/pick/preview` `{"part", "tol"}` take the same). Larger parts than a foam block are
fine — the size replaces the fixed gate — but the part must still be white to the camera.

## Build it

A JDK (11+; it builds Java 8 bytecode with `--release 8`) and Docker:

    make urcap5-sdk        # copy the URCap API jars out of the URSim e-Series image into target/
    make urcap5-package    # compile, write the manifest, zip → ../dist/*.urcap (commit it)

`urcap/urcap5.py` does what UR's Maven SDK would: `Import-Package` comes from the classes
(`jdeps`), the manifest carries the `URCapCompatibility-CB3` / `-eSeries` flags PolyScope
5's loader requires, and `META-INF/maven/**/pom.xml` names the `com.ur.urcap:api`
version (where PolyScope reads it). The API jars are UR's and are never committed. The
jar is reproducible, and carries a digest of its sources so a test flags a stale
`dist/` without a JDK.

## Status (2026-09-27)

- Compiles against PolyScope 5.26's own URCap API bundles; the bundle's manifest, pom and
  imports are tested; the client's URL rules, JSON, target text, error messages, and its
  HTTP against the real cockpit server are tested under a JDK.
- `get_inverse_kin_has_solution` / `get_inverse_kin` with `tcp=` verified on the UR3e
  (PolyScope 5.25.1).
- **Loaded in URSim 5.26** (2026-09-27, `scripts/ursim-e-vm.sh`: the amd64 image in an x86_64
  QEMU VM on the Mac): the bundle resolves and PolyScope registers the URCap, so the
  manifest, the embedded pom's API version (1.9.0) and the `[1.0.0,2.0.0)` import ranges are
  accepted. That load caught the one PolyScope-5-only bug so far: its own panel refuses
  `setBorder` (`AuthorizationException: Method not supported from URCaps`); the node now
  builds inside a panel it owns. **The node's screen has not been seen rendering yet** —
  PolyScope's JVM crashes in JIT-compiled code under emulation — so the first look at it is
  on the robot (`scripts/urcap5-usb.sh`, then the install steps above).

## Tested PolyScope versions

Every change to the URCap runs on the newest patch of each of the newest three PolyScope 5
minors, in URSim on GitHub's amd64 runners (`.github/workflows/urcap5-matrix.yml`;
`docker-compose.ps5-matrix.yml`, `urcap/ps5_matrix.py`):

| PolyScope | URSim image | Result (2026-09-28, URCap 0.4.0) |
| --------- | ----------- | -------------------------------- |
| 5.24 | `ursim_e-series:5.24.0` | bundle Active, both node services registered, pick e2e PASS |
| 5.25 | `ursim_e-series:5.25.2` | bundle Active, both node services registered, pick e2e PASS |
| 5.26 | `ursim_e-series:5.26.1` | bundle Active, both node services registered, pick e2e PASS |

Per version: the committed `dist/` jar goes in `/urcaps`; the check asks PolyScope's own
Felix shell (`127.0.0.1:6666` inside the container) for the bundle's state and the
services its activator registered, since polyscope.log never says a URCap started; any
polyscope.log line or stack frame naming the URCap with a failure fails the run. Then the
arm is brought up and the Pick node's own URScript runs on that controller against a pick
server on the runner (`urcap/pick5_e2e.py`: FIND → closer look → REFINE → grip → child
nodes → lift). What this does not cover: the node's screens rendering on a pendant.

A weekly job fails when Docker Hub has a newer 5.x minor or patch than the matrix
(`python3 urcap/ps5_matrix.py check-tags`: "PolyScope 5.27 exists: add it and drop 5.24").
On an amd64 Linux host: `make urcap5-matrix PS5_VERSION=5.26` (or `all`).
