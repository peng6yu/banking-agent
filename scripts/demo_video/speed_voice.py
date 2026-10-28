# -*- coding: utf-8 -*-
"""把旁白按统一变速系数 atempo 加速，产出新的旁白目录与 meta.json。

为什么必须单独一步（踩过的坑）：在 ffmpeg 的 filter_complex 里把 43 路旁白
和 atempo 一起混音，输出会变成空音轨（duration=N/A、文件只有 1KB 出头）。
所以先逐条变速落盘，再让 mux_final.py 只做 adelay 铺轨。

用法：.venv/bin/python scripts/demo_video/speed_voice.py [scale]
  scale 省略时取 tmp/cues.json 里的 scale（由 apply_scale.py 写入）
产出：tmp/voice5/*.mp3 + tmp/voice5/meta.json（total / scale / items）
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "tmp/voice4"
DST = ROOT / "tmp/voice5"
CUES = ROOT / "tmp/cues.json"


def dur_of(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True).stdout.strip()
    return float(out)


def main() -> None:
    if not (SRC / "meta.json").exists():
        raise SystemExit("缺少 tmp/voice4/meta.json，先跑 synth_edge.py")
    meta = json.loads((SRC / "meta.json").read_text())["items"]
    data = json.loads(CUES.read_text())
    scale = float(sys.argv[1]) if len(sys.argv) > 1 else float(data.get("scale", 1.0))
    if scale <= 0:
        raise SystemExit("scale 必须为正数")
    DST.mkdir(parents=True, exist_ok=True)
    items = {}
    total = 0.0
    for key, m in meta.items():
        src = SRC / m["file"]
        dst = DST / m["file"]
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter:a", f"atempo={scale}", "-q:a", "4", str(dst)],
            check=True)
        d = dur_of(dst)
        total += d
        items[key] = {"text": m["text"], "dur": round(d, 2), "file": m["file"],
                      "chars": m.get("chars"), "orig_dur": m.get("dur")}
    (DST / "meta.json").write_text(json.dumps(
        {"total": round(total, 2), "scale": scale, "items": items}, ensure_ascii=False, indent=1))
    print("voice5 meta 写入：%d 条，总长 %.2fs（scale=%.4f）" % (len(items), total, scale))


if __name__ == "__main__":
    main()
