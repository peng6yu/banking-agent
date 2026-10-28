"""给录屏结果打上统一变速系数：视频 setpts、旁白 atempo、字幕时间轴一起按系数缩放。

成片要卡在 5 分钟以内，但内容一条都不删；这里对整片（含旁白与字幕）统一加速，
画面、声音、字幕三者仍然严格对齐。

用法：.venv/bin/python scripts/demo_video/apply_scale.py 1.14
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CUES = ROOT / "tmp/cues.json"


def main() -> None:
    scale = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
    if scale <= 0:
        raise SystemExit("scale 必须为正数")
    data = json.loads(CUES.read_text())
    data["scale"] = round(scale, 4)
    CUES.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    dur = data["duration"] / scale
    print("scale=%.4f  成片时长 %.2fs (%.2f 分)" % (scale, dur, dur / 60))


if __name__ == "__main__":
    main()
