# -*- coding: utf-8 -*-
"""把交付视频的新数字（时长/字节/条数/覆盖点）同步到 README 与交付文档。

用法：成片落到 docs/交付/ 之后运行：.venv/bin/python scripts/demo_video/update_docs_video.py

v8 起改成「按旧串替换」而不是逐条硬编码整行：
- 数字用正则统一替换（时长两种写法、字节数、旁白条数、字幕段数）；
- 覆盖口径 FLOW 是一整段文字，三份文档里完全一致，整段换掉即可。
"""
import json, re, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MP4 = ROOT / "docs/交付/演示录屏_全功能.mp4"
SRT = ROOT / "docs/交付/演示录屏_字幕.srt"
DOCS = ["README.md", "docs/提交物对照表.md", "docs/交付/比赛演示方案.md"]
TICK = chr(96)

dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(MP4)], capture_output=True, text=True).stdout.strip())
size = MP4.stat().st_size
segs = len([b for b in SRT.read_text(encoding="utf-8").strip().split("\n\n") if b.strip()])
cues = len(json.loads((ROOT / "tmp/cues.json").read_text(encoding="utf-8"))["cues"])
mmss = f"{int(dur)//60}:{int(dur)%60:02d}"
sec = f"{dur:.2f}"
print(f"duration={sec}s ({mmss})  size={size:,} B  cues={cues}  segs={segs}")

# v8 的覆盖口径：先讲结构，再逐个功能真操作；计划类功能的主入口是「跟助手说一句话」。
FLOW = ("43 个功能点逐个真操作（画面上真做一遍，不只念功能名）："
        "先讲系统五层架构（接入 / 认知 / 裁决 / 执行 / 账本 + 三条流 + 界面分层 + 代码层号）"
        " → 助手五面板 + 三色闸门（绿灯直答 / 黄灯同名消歧 + 确认卡 / 红灯三因子令牌 / 超范围拒答）"
        " → 账务三页（总览待办 / 账单统计口径 / 异常规则与误报 / 逐笔核实 / 流水筛选与加载 / 手动记账）"
        " → 计划十件事（计划页四块总览 / 跟助手说一句话建储蓄目标 / 助手算每月要存多少还差多少 /"
        " 目标回计划页并能推进跨场景计划 / 说一句话建提醒 / 手动表单建储蓄计划 / 模拟支出 / 监测告警 / 未来 7 天排期）"
        " → 业务管理五件事（卡片限额与挂失 / 订阅推断与定性 / 长期记忆 / 适当性问卷 / 能力边界）"
        " → 治理五件事（执行记录 / 拒绝原因码 / 人工接管队列 / 审计链四元组哈希 / 改库篡改后再校验失败）"
        " → 收尾给系统自检数字")

# 旧 FLOW：从文档里原样取出来再换，避免手抄错字。
readme = (ROOT / "README.md").read_text(encoding="utf-8")
m = re.search(r"43 个功能点逐个真操作（画面上真做一遍，不只念功能名）：.+?自检数字", readme, re.S)
OLD_FLOW = m.group(0) if m else ""

NUM_RULES = [
    (r"4:42（282\.88s）", f"{mmss}（{sec}s）"),
    (r"4:42 / 282\.88s", f"{mmss} / {sec}s"),
    (r"32,897,197 B", f"{size:,} B"),
    (r"43 条旁白", f"{cues} 条旁白"),
    (r"52 段单行", f"{segs} 段单行"),
    (r"record_v7\.py", "record_v8.py"),
    (r"story2\.py", TICK + "story_v8.py" + TICK + " + " + TICK + "synth_edge_v8.py" + TICK),
]

for rel in DOCS:
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    before = s
    if OLD_FLOW in s:
        s = s.replace(OLD_FLOW, FLOW)
    else:
        print(f"  ! {rel} 里没有旧 FLOW 段落，跳过口径替换")
    for pat, new in NUM_RULES:
        s, n = re.subn(pat, new.replace("\\", "\\\\"), s)
        if n:
            print(f"  {rel}: {pat} -> {new}  x{n}")
    if s != before:
        p.write_text(s, encoding="utf-8")
        print("updated", rel)
    else:
        print("unchanged", rel)