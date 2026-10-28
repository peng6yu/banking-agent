"""把一次录下来的 webm、预合成的旁白、按真实时间轴生成的 SRT 合成交付版 mp4。

用法：.venv/bin/python scripts/demo_video/mux_final.py
"""
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
META = json.loads((ROOT / "tmp/voice4/meta.json").read_text())["items"]
VOICE = ROOT / "tmp/voice4"
DATA = json.loads((ROOT / "tmp/cues.json").read_text())
VIDEO = DATA["video"]
SHIFT = DATA["shift"]
CUES = DATA["cues"]
DUR = DATA["duration"]
OUT = ROOT / "tmp/final_silent.mp4"
SUB = ROOT / "tmp/final.srt"
FINAL = ROOT / "tmp/final_voice_subs.mp4"


def ts(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


lines = []
for n, cue in enumerate(CUES, 1):
    start = max(0.0, cue["start"] + SHIFT)
    end = max(start + 0.8, cue["end"] + SHIFT)
    lines += [str(n), f"{ts(start)} --> {ts(end)}", cue["text"], ""]
SUB.write_text("\n".join(lines), encoding="utf-8")
print(f"srt: {len(CUES)} 条 -> {SUB}")

# 1) webm -> mp4（无音轨）
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", VIDEO, "-c:v", "libx264", "-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p", "-an", str(OUT)], check=True)
print("video ok")

# 2) 旁白按各 cue 起点铺到一条音轨上
inputs = ["-i", str(OUT)]
filters = []
labels = []
for n, cue in enumerate(CUES, 1):
    meta = META[cue["key"]]
    inputs += ["-i", str(VOICE / meta["file"])]
    delay = int(round(max(0.0, cue["start"] + SHIFT) * 1000))
    filters.append(f"[{n}:a]adelay={delay}|{delay},volume=1.0[a{n}]")
    labels.append(f"[a{n}]")
filters.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,apad[aout]")
mix = ROOT / "tmp/final_audio.m4a"
subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(filters), "-map", "[aout]", "-t", f"{DUR:.3f}", "-c:a", "aac", "-b:a", "128k", str(mix)], check=True)
print("audio ok")

# 3) subtitles are burned by scripts/demo_video/mux_subs3.py (drawtext); SRT via subtitles= gets scaled badly
print('srt + silent + audio ready; run scripts/demo_video/mux_subs3.py next')
