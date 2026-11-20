#!/usr/bin/env python3
"""Render demo/transcript.json into an annotated terminal video (mp4).

The video is drawn from the already-redacted transcript, so nothing that is not in the
transcript (and the captions written in record.py) can appear on screen. There is no
narration: every step carries a caption explaining what is happening.

    python demo/render_video.py [--in demo/transcript.json] [--out demo/demo.mp4]

Needs: Pillow, ffmpeg on PATH, and a monospace font (Menlo on macOS, DejaVu Sans Mono
on Linux).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap

from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1280, 720, 12
BG, TERM_BG = (18, 20, 28), (13, 15, 22)
FG, DIM = (214, 220, 232), (130, 138, 156)
GREEN, AMBER, RED, BLUE = (126, 214, 135), (255, 190, 90), (255, 120, 120), (120, 190, 255)
CAP_BG, CAP_FG = (30, 64, 120), (245, 248, 255)
PAD, CAP_H, BAR_H = 28, 112, 30

MONO_CANDIDATES = [
    ("/System/Library/Fonts/Menlo.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0),
]
SANS_CANDIDATES = [
    ("/System/Library/Fonts/Helvetica.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
]


def load_font(cands, size):
    for path, idx in cands:
        if os.path.exists(path):
            return ImageFont.truetype(path, size, index=idx)
    return ImageFont.load_default()


MONO = load_font(MONO_CANDIDATES, 17)
MONO_B = load_font(MONO_CANDIDATES, 17)
SANS = load_font(SANS_CANDIDATES, 27)
SANS_S = load_font(SANS_CANDIDATES, 19)
SANS_XL = load_font(SANS_CANDIDATES, 46)

CHAR_W = MONO.getlength("M")
COLS = int((W - 2 * PAD) // CHAR_W)
LINE_H = 24
TERM_TOP = CAP_H + BAR_H
ROWS = (H - TERM_TOP - 16) // LINE_H


def wrap_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.rstrip("\n").split("\n"):
        if len(raw) <= COLS:
            out.append(raw)
        else:
            indent = len(raw) - len(raw.lstrip())
            out.extend(textwrap.wrap(raw, COLS, subsequent_indent=" " * min(indent + 2, 8),
                                     break_long_words=True, replace_whitespace=False) or [""])
    return out


def colour_for(line: str):
    s = line.lstrip()
    if "WARNING" in s or "Insufficient" in s or "Pending" in s:
        return AMBER
    if s.startswith(("Error", "error")) or "FAILED" in s:
        return RED
    if s.startswith(("NOTE", "- **NOTE", "**Recommended", "Wrote")) or "NOTE -" in s:
        return BLUE
    return FG


class Pipe:
    def __init__(self, path: str):
        self.p = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-crf", "20", "-preset", "medium", "-movflags", "+faststart", path],
            stdin=subprocess.PIPE)
        self.frames = 0

    def put(self, img: Image.Image, seconds: float):
        data = img.tobytes()
        for _ in range(max(1, round(seconds * FPS))):
            self.p.stdin.write(data)
            self.frames += 1

    def close(self):
        self.p.stdin.close()
        self.p.wait()


def center(d, text, font, y, fill):
    w = d.textlength(text, font=font)
    d.text(((W - w) / 2, y), text, font=font, fill=fill)


def card(title: str, lines: list[str]) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    center(d, title, SANS_XL, 190, (240, 244, 255))
    y = 290
    for ln in lines:
        center(d, ln, SANS, y, (170, 190, 225))
        y += 42
    return img


def frame(caption: str, step_label: str, term: list[tuple[str, tuple]], cursor: bool) -> Image.Image:
    img = Image.new("RGB", (W, H), TERM_BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, CAP_H], fill=CAP_BG)
    d.text((PAD, 14), step_label, font=SANS_S, fill=(160, 195, 255))
    y = 44
    for ln in textwrap.wrap(caption, 82)[:2]:
        d.text((PAD, y), ln, font=SANS, fill=CAP_FG)
        y += 36
    d.rectangle([0, CAP_H, W, CAP_H + BAR_H], fill=(36, 40, 54))
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([16 + i * 22, CAP_H + 8, 28 + i * 22, CAP_H + 20], fill=c)
    d.text((W / 2 - 150, CAP_H + 5), "kube-sched-advisor  |  Amazon EKS demo", font=SANS_S, fill=DIM)
    y = TERM_TOP + 10
    for text, colour in term[-ROWS:]:
        d.text((PAD, y), text, font=MONO, fill=colour)
        y += LINE_H
    if cursor and term:
        last = term[-ROWS:][-1][0]
        cy = TERM_TOP + 10 + (len(term[-ROWS:]) - 1) * LINE_H
        cx = PAD + CHAR_W * len(last)
        d.rectangle([cx, cy + 2, cx + CHAR_W * 0.6, cy + LINE_H - 4], fill=(200, 205, 220))
    return img


def read_seconds(text: str, minimum: float, wps: float = 2.8) -> float:
    return max(minimum, len(text.split()) / wps)


def render(transcript: dict, out: str):
    pipe = Pipe(out)
    intro = transcript["intro"]
    pipe.put(card(intro["title"], intro["lines"]), intro.get("seconds", 7))
    steps = transcript["steps"]
    for n, st in enumerate(steps, 1):
        label = f"Step {n} of {len(steps)}"
        cap = st["caption"]
        cap_t = read_seconds(cap, 3.0)
        prompt = "$ "
        cmd_lines = wrap_lines(prompt + st["command"])
        term: list[tuple[str, tuple]] = []

        # caption alone first, so it can be read before anything moves
        pipe.put(frame(cap, label, [(prompt, GREEN)], True), min(cap_t * 0.45, 4.0))

        # type the command
        typed = ""
        full = prompt + st["command"]
        step = 3
        for i in range(len(prompt), len(full) + 1, step):
            typed = full[:i]
            term = [(ln, GREEN) for ln in wrap_lines(typed)]
            pipe.put(frame(cap, label, term, True), 1 / FPS)
        term = [(ln, GREEN) for ln in cmd_lines]
        pipe.put(frame(cap, label, term, True), 0.6)

        # reveal output
        out_lines = wrap_lines(st["output"]) if st["output"].strip() else []
        per_frame = 3
        for i in range(0, len(out_lines), per_frame):
            chunk = out_lines[i:i + per_frame]
            term = term + [(ln, colour_for(ln)) for ln in chunk]
            pipe.put(frame(cap, label, term, False), 1 / FPS)

        hold = max(cap_t * 0.55, 2.5, len(out_lines) * 0.22)
        hold = max(hold, st.get("min_hold", 0))
        pipe.put(frame(cap, label, term, False), min(hold, 14))
    outro = transcript["outro"]
    pipe.put(card(outro["title"], outro["lines"]), outro.get("seconds", 8))
    pipe.close()
    return pipe.frames / FPS


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--in", dest="inp", default=os.path.join(here, "transcript.json"))
    ap.add_argument("--out", default=os.path.join(here, "demo.mp4"))
    a = ap.parse_args()
    with open(a.inp) as fh:
        t = json.load(fh)
    secs = render(t, a.out)
    print(f"wrote {a.out} ({secs:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
