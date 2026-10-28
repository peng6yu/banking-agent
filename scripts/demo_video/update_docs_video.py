# -*- coding: utf-8 -*-
"""把交付视频的新数字（时长/字节/条数/覆盖点）同步到 README 与交付文档。

用法：成片落到 docs/交付/ 之后运行：.venv/bin/python scripts/demo_video/update_docs_video.py
"""
import json, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MP4 = ROOT / "docs/交付/演示录屏_全功能.mp4"
SRT = ROOT / "docs/交付/演示录屏_字幕.srt"

dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(MP4)], capture_output=True, text=True).stdout.strip())
size = MP4.stat().st_size
segs = len([b for b in SRT.read_text(encoding="utf-8").strip().split("\n\n") if b.strip()])
cues = len(json.loads((ROOT / "tmp/cues.json").read_text(encoding="utf-8"))["cues"])
mmss = f"{int(dur)//60}:{int(dur)%60:02d}"
sec = f"{dur:.2f}"
print(f"duration={sec}s ({mmss})  size={size:,} B  cues={cues}  segs={segs}")

ARCH = "系统五层架构总览（接入 / 认知 / 裁决 / 执行 / 账本 + 三条流 + 界面分层对照 + 代码层号 + 系统自检数字）"

EDITS = {
    "README.md": [
        ("4:30（269.80s）/ 1440×900", f"{mmss}（{sec}s）/ 1440×900"),
        ("演示与机制同步讲：认知层边界 →", f"演示与机制同步讲：{ARCH} → 认知层边界 →"),
        ("（23 条旁白 / 59 段单行字幕，无重复）", f"（{cues} 条旁白 / {segs} 段单行字幕，无重复）"),
    ],
    "docs/提交物对照表.md": [
        ("4:30 / 269.80s / 1440×900 / 23 条旁白 / 59 段单行字幕无重复，旁白逐段讲机制",
         f"{mmss} / {sec}s / 1440×900 / {cues} 条旁白 / {segs} 段单行字幕无重复，旁白逐段讲机制"),
        ("4:30（269.80s），1440×900，h264+aac，20,731,494 B；23 条旁白 / 59 段单行字幕无重复。",
         f"{mmss}（{sec}s），1440×900，h264+aac，{size:,} B；{cues} 条旁白 / {segs} 段单行字幕无重复。"),
        ("覆盖：开场先讲认知层（模型只提申请，不碰余额、不写库、不调工具）→",
         f"覆盖：{ARCH} → 开场先讲认知层（模型只提申请，不碰余额、不写库、不调工具）→"),
        ("4:30 / 269.80s / 1440×900 / 23 条旁白 / 59 段单行字幕无重复",
         f"{mmss} / {sec}s / 1440×900 / {cues} 条旁白 / {segs} 段单行字幕无重复"),
    ],
    "docs/交付/比赛演示方案.md": [
        ("4:30（269.80s），1440×900，字幕 `演示录屏_字幕.srt` 59 段单行、无重复",
         f"{mmss}（{sec}s），1440×900，字幕 `演示录屏_字幕.srt` {segs} 段单行、无重复"),
        ("覆盖开场 → 绿灯查账单", "覆盖架构总览（五层 + 三条流 + 界面分层 + 系统自检数字）→ 开场 → 绿灯查账单"),
    ],
}

for rel, edits in EDITS.items():
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    for old, new in edits:
        if old not in s:
            raise SystemExit(f"NOT FOUND in {rel}: {old[:50]}")
        s = s.replace(old, new)
    p.write_text(s, encoding="utf-8")
    print("updated", rel)
