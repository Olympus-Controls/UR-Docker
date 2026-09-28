"""Timelapse: webcams on top, wrist camera below, narrated captions burned in.

usage: assemble.py <rec dir> <out.mp4> [speed]

Events that carry plain words (``think`` / ``do``, pick-cycle's narration) are
captioned as two lines — THINKING first, DOING joins it a beat later — for a lay
audience; a recording without them falls back to each event's ``text``. The
speed ramps: ``speed``× between captions, but every caption stays up at least as
long as it takes to read, so a quick phase plays slower instead of flashing by.
"""
# ruff: noqa  — scratch script from the 2026-09-25 hardware session (scripts/pilot/README.md)

import bisect
import json
import os
import shutil
import subprocess
import sys

REC, OUT = sys.argv[1], sys.argv[2]
SPEED = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
FPS_OUT = 20.0
W = 1280
BAND = 190  # caption band under the pictures
FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

idx = json.load(open(REC + "/index.json"))
events = json.load(open(REC + "/events.json"))
t_end = max(t for k in idx for t, _ in idx[k])

lay = [e for e in events if e.get("think") or e.get("do")]
caps = lay or [{"t": e["t"], "do": e["text"]} for e in events]


def read_s(e):
    words = len((e.get("think", "") + " " + e.get("do", "")).split())
    return 1.5 + 0.33 * words  # ~180 wpm plus a moment to find the line


# segments of real time → output durations (the speed ramp)
bounds = [0.0] + [c["t"] for c in caps] + [t_end]
segs = []  # (real t0, real t1, out duration, caption or None)
for i in range(len(bounds) - 1):
    r0, r1 = bounds[i], bounds[i + 1]
    cap = caps[i - 1] if i > 0 else None
    dur = (r1 - r0) / SPEED
    if cap is not None:
        dur = max(dur, read_s(cap))
    if r1 - r0 > 0.01 or cap is not None:
        segs.append((r0, max(r1, r0 + 0.01), dur, cap))

# frame times: each output frame samples real time linearly inside its segment
frame_t, cap_spans, t_out = [], [], 0.0
for r0, r1, dur, cap in segs:
    n = max(1, round(dur * FPS_OUT))
    frame_t += [r0 + (r1 - r0) * k / n for k in range(n)]
    if cap is not None:
        cap_spans.append((cap, t_out, t_out + n / FPS_OUT))
    t_out += n / FPS_OUT

seqdir = REC + "/seq"
shutil.rmtree(seqdir, ignore_errors=True)
for k in ("v0", "v1", "c"):
    os.makedirs(f"{seqdir}/{k}")
    times = [t for t, _ in idx[k]]
    names = [nm for _, nm in idx[k]]
    for f, t in enumerate(frame_t):
        j = max(0, bisect.bisect_right(times, t) - 1)
        os.link(names[j], f"{seqdir}/{k}/{f:06d}{os.path.splitext(names[j])[1]}")


def line_png(path, label, text, colour):
    """One caption line: a small bold label, then the words, wrapped to the band."""
    subprocess.run(
        [
            "magick",
            "-background",
            "none",
            "(",
            "-fill",
            colour,
            "-font",
            BOLD,
            "-pointsize",
            "20",
            f"label:{label}",
            ")",
            "(",
            "-fill",
            "white",
            "-font",
            FONT,
            "-pointsize",
            "30",
            "-size",
            f"{W - 260}x",
            f"caption:{text}",
            ")",
            "-gravity",
            "northwest",
            "-background",
            "none",
            "+append",
            path,
        ],
        check=True,
    )


capdir = REC + "/cap"
shutil.rmtree(capdir, ignore_errors=True)
os.makedirs(capdir)
overlays = []  # (png, t0, t1)
for i, (cap, t0, t1) in enumerate(cap_spans):
    think, do = cap.get("think"), cap.get("do")
    parts = []
    if think:
        line_png(f"{capdir}/{i:03d}t.png", "THINKING   ", think, "#FFC857")
        parts.append(f"{capdir}/{i:03d}t.png")
    if do:
        line_png(f"{capdir}/{i:03d}d.png", "DOING        ", do, "#7FDBFF")
        parts.append(f"{capdir}/{i:03d}d.png")
    # think alone first, then think + do: the "then doing" beat
    split = t0 + (t1 - t0) * 0.4 if (think and do) else t0
    if think and do:
        overlays.append((parts[0], t0, split))
    both = f"{capdir}/{i:03d}b.png"
    subprocess.run(
        ["magick", "-background", "none", *parts, "-gravity", "west", "-splice", "0x0", "-append", both],
        check=True,
    )
    overlays.append((both, split, t1))

badge = f"{capdir}/badge.png"
subprocess.run(
    [
        "magick",
        "-background",
        "none",
        "-fill",
        "#FFFFFF90",
        "-font",
        BOLD,
        "-pointsize",
        "18",
        f"label:timelapse, up to {SPEED:g}x",
        badge,
    ],
    check=True,
)
inputs = []
for k, ext in (("v0", "jpg"), ("v1", "jpg"), ("c", "png")):
    inputs += ["-framerate", str(FPS_OUT), "-i", f"{seqdir}/{k}/%06d.{ext}"]
inputs += ["-loop", "1", "-framerate", str(FPS_OUT), "-i", badge]
for png, _, _ in overlays:
    inputs += ["-loop", "1", "-framerate", str(FPS_OUT), "-i", png]
H = 960 + BAND
fg = (
    "[0:v]scale=640:480[a];[1:v]scale=640:480[b];[a][b]hstack[top];"
    "[2:v]scale=848:480,pad=1280:480:216:0:black[bot];"
    f"[top][bot]vstack,pad={W}:{H}:0:0:black[bare];"
    "[bare][3:v]overlay=W-w-16:H-h-10:shortest=1[base]"
)
cur = "base"
for k, (png, t0, t1) in enumerate(overlays):
    fg += (
        f";[{cur}][{4 + k}:v]overlay=40:960+({BAND}-h)/2:"
        f"enable='between(t,{t0:.3f},{t1 - 0.001:.3f})':shortest=1[o{k}]"
    )
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
    len(frame_t),
    "real",
    round(t_end, 1),
    "s → video",
    round(t_out, 1),
    "s; captions",
    len(cap_spans),
    "(narrated)" if lay else "(engineer's lines)",
)
