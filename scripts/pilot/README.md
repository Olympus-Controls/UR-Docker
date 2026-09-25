# scripts/pilot — the 2026-09-25 UR3e + Hand-E session scripts

Working scripts from the first hardware pick (see `docs/realsense.md` §Hand-eye
without a mark and §Picking with the Hand-E). They drive a **running cockpit**
(`perception --cell ur3 gui --rs-lean`, port 7621) over its HTTP API and the
gripper through `urctl gripper`; nothing here talks to the robot directly.
Scratch quality, kept because they worked; the plan is to fold them into
`perception` commands (`TODO.md`).

| script | what |
| --- | --- |
| `orbit_cal6.py` (uses `orbit_cal4.py`/`orbit_cal.py` helpers) | mark-less hand-eye: orbit a block's top-face centre at three ranges, plane-fit clicks, identity-tracked views, `POST /api/cal/view` per view, solve |
| `pick.py plan [x y] / hover / descend mm / grip / release / lift mm / move dx dy` | the step-wise pick; every step snapshots all cameras |
| `place.py [--go]` | survey the other blocks, choose a clear spot, set the held block down |
| `wiggle.py NAME [dx dy dz]` | jog + re-measure the block offset from the finger axis |
| `record.py DIR` / `show.py DIR` / `assemble.py DIR out.mp4 [speed]` | record the three feeds, run the captioned pick-and-shuffle, build the subtitled timelapse (ffmpeg + ImageMagick) |

Run from the repo root with `uv run python scripts/pilot/<script>.py …`.
