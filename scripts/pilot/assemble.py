"""Timelapse: webcams on top, wrist camera below, captions burned in. usage: assemble.py <rec dir> <out.mp4> [speed]"""
# ruff: noqa  — scratch script from the 2026-09-25 hardware session (scripts/pilot/README.md)

import os as _os

HERE = _os.path.dirname(_os.path.abspath(__file__))

import bisect
import json
import os
import shutil
import subprocess
import sys

REC, OUT = sys.argv[1], sys.argv[2]
SPEED = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
FPS_REAL = 5.0
FPS_OUT = FPS_REAL * SPEED  # sample real time at 5 Hz, play at 5*speed → speed× timelapse
idx = json.load(open(REC + "/index.json"))
events = json.load(open(REC + "/events.json"))
t_end = max(t for k in idx for t, _ in idx[k])
n = int(t_end * FPS_REAL)
seqdir = REC + "/seq"
shutil.rmtree(seqdir, ignore_errors=True)
[os.makedirs(f"{seqdir}/{k}") for k in ("v0", "v1", "c")]
for k in ("v0", "v1", "c"):
    times = [t for t, _ in idx[k]]
    names = [nm for _, nm in idx[k]]
    for f in range(n):
        t = f / FPS_REAL
        j = bisect.bisect_right(times, t) - 1
        j = max(0, j)
        os.link(names[j], f"{seqdir}/{k}/{f:06d}{os.path.splitext(names[j])[1]}")
# captions → transparent PNGs via ImageMagick, overlaid on the output timeline (t_out = t_real / speed)
capdir = REC + "/cap"
shutil.rmtree(capdir, ignore_errors=True)
os.makedirs(capdir)
W = 1280
overlays = []
for i, e in enumerate(events):
    t0 = e["t"] / SPEED
    t1 = (events[i + 1]["t"] / SPEED) if i + 1 < len(events) else t_end / SPEED + 1
    png = f"{capdir}/{i:02d}.png"
    subprocess.run(
        [
            "magick",
            "-background",
            "#000000B0",
            "-fill",
            "white",
            "-font",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "-pointsize",
            "30",
            "-size",
            f"{W - 40}x",
            "-gravity",
            "center",
            f"caption:{e['text']}",
            "-bordercolor",
            "#000000B0",
            "-border",
            "20x12",
            png,
        ],
        check=True,
    )
    overlays.append((png, t0, t1))
# filter graph: [v0][v1] hstack → 1280x480 ; wrist 848x480 → pad to 1280 ; vstack ; pad 90 px at the bottom for captions ; overlays
inputs = [
    "-framerate",
    str(FPS_OUT),
    "-i",
    f"{seqdir}/v0/%06d.jpg",
    "-framerate",
    str(FPS_OUT),
    "-i",
    f"{seqdir}/v1/%06d.jpg",
    "-framerate",
    str(FPS_OUT),
    "-i",
    f"{seqdir}/c/%06d.png",
]
for png, _, _ in overlays:
    inputs += ["-loop", "1", "-i", png]
fg = "[0:v]scale=640:480[a];[1:v]scale=640:480[b];[a][b]hstack[top];[2:v]scale=848:480,pad=1280:480:216:0:black[bot];[top][bot]vstack,pad=1280:1050:0:0:black[base]"
cur = "base"
for k, (png, t0, t1) in enumerate(overlays):
    fg += f";[{cur}][{3 + k}:v]overlay=(W-w)/2:960-h/2:enable='between(t,{t0:.2f},{t1:.2f})':shortest=1[o{k}]"
    cur = f"o{k}"
cmd = [
    "ffmpeg",
    "-y",
    "-hide_banner",
    "-loglevel",
    "error",
    *inputs,
    "-filter_complex",
    fg,
    "-map",
    f"[{cur}]",
    "-r",
    str(FPS_OUT),
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-crf",
    "20",
    "-movflags",
    "+faststart",
    OUT,
]
subprocess.run(cmd, check=True)
print(
    "wrote",
    OUT,
    "frames",
    n,
    "real",
    round(t_end, 1),
    "s → video",
    round(t_end / SPEED, 1),
    "s at",
    FPS_OUT,
    "fps; captions",
    len(events),
)
