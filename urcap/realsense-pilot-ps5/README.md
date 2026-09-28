# RealSense Pilot for PolyScope 5 (e-Series)

The same operator tool as the PolyScope X node in `../realsense-pilot/`, for an e-Series
robot on PolyScope 5: the wrist RealSense feed inside PolyScope, tap a point, and send
the arm there. It is an **Installation node** (Installation tab → URCaps → RealSense
Pilot), the PolyScope 5 counterpart of PolyScope X's Application node.

Download: [`../dist/realsense-pilot-ps5-0.3.0.urcap`](../dist/realsense-pilot-ps5-0.3.0.urcap)

## Install on the robot

1. Copy `realsense-pilot-ps5-0.3.0.urcap` to a USB stick and plug it into the pendant.
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
