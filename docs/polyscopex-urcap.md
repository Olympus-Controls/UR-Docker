# RealSense Pilot — a PolyScope X URCap that embeds the wrist camera

`urcap/realsense-pilot/` is a **URCap X** (PolyScope 10) Application Node: the
RealSense colour feed inside PolyScope's own UI, hover-to-measure, and a click
that becomes a base-frame point and an approach pose through the cockpit's
hand-eye — then a move, either through PolyScope's auto-move screen (operator
holds to move) or through the cockpit over Primary (urctl's safety envelope).
It is plain JavaScript: **no Angular, no webpack, no npm** — `scripts/urcapx.py`
packages and installs it with the stdlib.

```
urcap/realsense-pilot/
  manifest.yaml                          vendorID olympus-controls, urcapID realsense-pilot
  realsense-pilot-frontend/
    contribution.json                    one applicationNode: tag olympus-realsense-pilot
    main.js                              the presenter (a custom element)
    realsense-pilot-node.worker.js       the behavior worker (node factory / upgrade)
    assets/i18n/en.json                  node title + supportive text
    assets/icons/realsense-pilot.svg
scripts/urcapx.py                        package | install | list | delete
```

## Why it is shaped like this (sourced from the SDK, 2026-09-26)

Facts from `PolyScopeX_URCap_SDK` 6.5.65 — the SDK that pairs with our simulator
image (`ursim_polyscopex:10.13.0` = `0.18.96`; `contribution-api` 21.3.266,
`urcap-utils` 2.1.2, manifest schema 19.10.31; SDK 6.6.66 at the repo's HEAD
targets 10.14):

- **A frontend may be any standards-compliant web component.** The generator's
  "javascript (expert mode)" template is `class X extends HTMLElement` +
  `customElements.define`. PolyScope sets `applicationNode`, `applicationAPI`,
  `robotSettings`, `robotContext` on the element; state is saved with
  `applicationAPI.applicationNodeService.updateNode(node)`.
- **The behavior worker is threads.js.** `registerApplicationBehavior(b)` is
  `expose(b)` from threads 1.7 (contribution-api fesm2022 ~line 6205), so the
  worker here speaks that protocol directly (`init` / `run` / `running` /
  `result` / `error` messages, verified against threads 1.7.0's
  `dist/worker/index.js`) — `tests/test_urcap.py` runs it under node.
- **`.urcapx` is a gzipped tar with `manifest.yaml` first** (`package-urcap.js`
  + `tar-helper.js`); `scripts/urcapx.py package` does the same.
- **Two install endpoints.** The SDK's CLI posts field `urcapx_file` to the
  Robot-API (`/universal-robots/robot-api/urcaps/v1/urcaps/`,
  `urservice-helper.js`), which answers **403 unless the robot is in Remote
  mode** — from inside the container too ("must be in remote mode or originate
  from an internal URCap"). PolyScope's own System Manager (an internal URCap)
  uses `/universal-robots/urservice/api/v1/urcaps` instead, and that one
  accepted the package from the host in Local mode (2026-09-26, 10.13.0 sim):
  multipart `POST` of field **`urcapxFile`** → 201, a duplicate → 409
  `already_installed`, `DELETE …/<vendor>/<urcap>` → 200, `GET` → a plain
  JSON array. `scripts/urcapx.py` uses that endpoint (`--replace` = delete +
  install; there is no update verb). The SDK's `run-simulator` sets
  `DEVMODE=true` for the Robot-API path; our compose passes `URSIM_PX_DEVMODE`
  through, but it only matters at container creation.
- **The page is same-origin with PolyScope** (nginx serves web archives from
  `/var/urcaps`; no CSP on the 10.13 sim), so `fetch` to another host works
  the way any page's does: the **cockpit must send CORS headers** for
  PolyScope's origin — `perception gui --cors http://<pendant-or-sim-host>:<port>`
  (`PERCEPTION_CORS`). The docs allow direct REST from a frontend on a real
  robot; the sim itself cannot reach external devices, but the *browser* can.
- **No frontend API runs URScript.** `ApplicationPresenterAPI` offers
  `robotPositionService.getInverseKinematics(pose, qNear)` and
  `robotMoveService.autoMove(joints)` (UR's hold-to-move screen), and there is
  no script endpoint in the Robot-API either. Hence the two Move buttons.

## Running the mock-up (sim on this Mac)

```bash
make simx-up                       # PolyScope X 10.13.0 on http://localhost:8000
make urcap-cockpit                 # a synthetic cockpit on :7622, CORS for the sim's origin
make urcap-install                 # package + install (no Remote mode needed; --replace if already there)
```

Then in PolyScope X: refresh the browser, **Application** → **RealSense
Pilot**. Set the cockpit URL to `http://localhost:7622` (Save; it persists in
the node), and the feed appears; hover reads depth, a click marks the pixel,
segments and calls locate. The fake cockpit has no robot behind it, so locate
answers "robot unreachable" there — the base point, approach pose, reach and
the Move buttons need a cockpit with a robot link (below).

For the real camera: restart your cockpit with CORS, e.g.

```bash
sudo .venv/bin/perception --cell ur3 gui --rs-lean --cors http://localhost:8000
```

and point the node at `http://localhost:7621`. Move (cockpit) then drives the
UR3e exactly as the cockpit's own Move does; Move (PolyScope) is only
meaningful on a PolyScope X controller (the sim's arm), where IK + auto-move act
on *that* robot.

## What is verified and what is not

| Verified (2026-09-26) | How |
| --- | --- |
| Source tree consistent (manifest ↔ contribution ↔ tag ↔ i18n) | `tests/test_urcap.py` |
| Package = gzipped tar, manifest first, LICENSE + frontend inside | test |
| Install: POST when absent, PUT when present, `--replace` deletes first, 403 named | test against a Robot-API look-alike |
| Worker speaks the threads protocol (init/running/result/error) | test under node |
| Cockpit CORS (allow-listed origins only, preflight, exposed `X-Seq`) + `GET /api/color.png` | test |
| The sim accepts the package (201), lists it, deletes it, serves the four frontend files | `scripts/urcapx.py install --replace` against `ursim_polyscopex:10.13.0` |
| The node loads in PolyScope X: worker + presenter fetched, element present, i18n title shown | headless Chromium on the sim (Playwright) |
| Feed live from a CORS-enabled cockpit, hover depth, click → segment → locate, cockpit URL persisted through `updateNode` and a reload | same, against `make urcap-cockpit` |
| Locate → base point / approach / reach, Move (cockpit) | **not yet** — needs a cockpit with a robot link (your live one with `--cors`) |
| Move (PolyScope): IK + auto-move accept the pose, `Pose.orientation` is a rotation vector | **not yet** — needs the PolyScope X arm powered and a located target |

Open: `Pose.orientation` for `getInverseKinematics` is assumed to be the UR
rotation vector (the SDK's `Pose` doc says only "x, y, z components"); a
mismatched convention shows up as the auto-move screen targeting a rotated
tool — compare against the cockpit's approach pose on first use.

## On the robot (later)

On the Jetson-next-to-a-PolyScope-X-robot deployment the cockpit runs on the
Jetson and the pendant's browser reaches it over the cell network — the same
URCap, CORS for the pendant's origin. The alternative the SDK offers is a
**backend container** declared in the manifest (`containers:` with
`devices: [{type: video}]` hot-plug hooks and `services: [urcontrol-primary]`),
which would put the cockpit inside PolyScope's Docker and reach the controller
on `urcontrol-primary:30001`; that is a packaging step on top of
`Dockerfile.perception`, not a rewrite.
