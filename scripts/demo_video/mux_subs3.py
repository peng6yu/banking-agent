"""单行字幕：一句一行，长句在句内按比例切成几段依次显示，画面上永远只有一行。"""
from __future__ import annotations
import json, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = json.loads((ROOT / "tmp/cues.json").read_text())
SHIFT, CUES = DATA["shift"], DATA["cues"]
SILENT = ROOT / "tmp/final_silent.mp4"
AUDIO = ROOT / "tmp/final_audio.m4a"
OUT = ROOT / "tmp/final_voice_subs.mp4"
SRT = ROOT / "tmp/final.srt"
TXT = ROOT / "tmp/subtxt3"
TXT.mkdir(exist_ok=True)
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
SIZE, LIMIT, BOTTOM = 25, 30, 828
MARKS = "，。；：、）」』？！—"


def split(text: str, limit: int = LIMIT) -> list[str]:
    text = text.strip()
    out: list[str] = []
    while len(text) > limit:
        window = text[: limit + 1]
        cut = max((i + 1 for i, ch in enumerate(window) if ch in MARKS), default=-1)
        if cut < limit * 0.5:
            cut = limit
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        if out and len(text) < 6:
            out[-1] += text
        else:
            out.append(text)
    return out or [""]


def ts(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


filters, srt, n = [], [], 0
for cue in CUES:
    segs = split(cue["text"])
    t0 = cue["start"] + SHIFT
    t1 = max(t0 + 1.0, cue["end"] + SHIFT)
    total = sum(len(s) for s in segs) or 1
    cur = t0
    for seg in segs:
        share = (t1 - t0) * len(seg) / total
        a, b = cur, cur + share
        cur = b
        n += 1
        f = TXT / ("%03d.txt" % n)
        f.write_text(seg, encoding="utf-8")
        rel = f.relative_to(ROOT)
        filters.append(
            "drawtext=fontfile=%s:textfile=%s:fontsize=%d:fontcolor=white:"
            "box=1:boxcolor=black@1:boxborderw=12:x=(w-text_w)/2:y=%d:enable='between(t,%.3f,%.3f)'"
            % (FONT, "'%s'" % rel, SIZE, BOTTOM, a, b)
        )
        srt += [str(n), "%s --> %s" % (ts(a), ts(b)), seg, ""]

(ROOT / "tmp/subs_filter.txt").write_text(",".join(filters), encoding="utf-8")
SRT.write_text("\n".join(srt), encoding="utf-8")
print("drawtext: %d 段 / %d 条 cue；最长一行 %d 字" % (len(filters), len(CUES), max(len(s) for s in [x["text"] for x in CUES])))
print("srt ->", SRT)

if "--burn" in __import__("sys").argv:
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(SILENT), "-i", str(AUDIO),
        "-filter_script:v", str(ROOT / "tmp/subs_filter.txt"),
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-movflags", "+faststart", str(OUT),
    ], check=True, cwd=str(ROOT))
    print("done", OUT)
    print(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size", "-of", "default=nw=1", str(OUT)], capture_output=True, text=True).stdout)