"""把一次录下来的 webm、预合成的旁白、按真实时间轴生成的 SRT 合成交付版 mp4。

用法：.venv/bin/python scripts/demo_video/mux_final.py
"""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VOICE = Path(os.environ.get("DSH_VOICE_DST", ROOT / "tmp/voice5"))  # 旁白已按 scale 预先变速（atempo），这里只做延时铺轨
META = json.loads((VOICE / "meta.json").read_text())["items"]
DATA = json.loads((ROOT / "tmp/cues.json").read_text())
VIDEO = DATA["video"]
SHIFT = DATA["shift"]
CUES = DATA["cues"]
DUR = DATA["duration"]
SCALE = float(DATA.get("scale", 1.0))  # 全片统一变速：画面/旁白/字幕一起缩放，保持对齐
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
    start = max(0.0, (cue["start"] + SHIFT) / SCALE)
    end = max(start + 0.8, (cue["end"] + SHIFT) / SCALE)
    lines += [str(n), f"{ts(start)} --> {ts(end)}", cue["text"], ""]
SUB.write_text("\n".join(lines), encoding="utf-8")
print(f"srt: {len(CUES)} 条 -> {SUB}")

# 1) webm -> mp4（无音轨）
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", VIDEO, "-vf", f"setpts=PTS/{SCALE}",
                "-c:v", "libx264", "-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p", "-an", str(OUT)], check=True)
print("video ok")

# 2) 旁白按各 cue 起点铺到一条音轨上
inputs = ["-i", str(OUT)]
filters = []
labels = []
for n, cue in enumerate(CUES, 1):
    meta = META[cue["key"]]
    inputs += ["-i", str(VOICE / meta["file"])]
    delay = int(round(max(0.0, (cue["start"] + SHIFT) / SCALE) * 1000))
    filters.append(f"[{n}:a]adelay={delay}|{delay},volume=1.0[a{n}]")
    labels.append(f"[a{n}]")
filters.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,apad[aout]")
mix = ROOT / "tmp/final_audio.m4a"
subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(filters), "-map", "[aout]", "-t", f"{DUR / SCALE:.3f}", "-c:a", "aac", "-b:a", "128k", str(mix)], check=True)
print("audio ok")

# 3) subtitles are burned by scripts/demo_video/mux_subs3.py (drawtext); SRT via subtitles= gets scaled badly
print('srt + silent + audio ready; run scripts/demo_video/mux_subs3.py next')
