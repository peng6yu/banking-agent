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

# v7 的覆盖口径：43 个功能点逐个真操作，而不是只念功能名。
FLOW = ("43 个功能点逐个真操作（画面上真做一遍，不只念功能名）："
        "助手五面板 + 三色闸门（绿灯直答 / 黄灯同名消歧 + 确认卡 / 红灯三因子令牌 / 超范围拒答）"
        " → 账务三页（总览待办 / 账单统计口径 / 异常规则与误报 / 逐笔核实 / 流水筛选与加载 / 手动记账）"
        " → 计划七件事（储蓄计划授权划款 / 储蓄目标预算告警 / 生日模板 / 提醒 / 监测 / 未来 7 天排期 / 跨场景续改）"
        " → 业务管理五件事（卡片限额与挂失 / 订阅推断与定性 / 长期记忆 / 适当性问卷 / 能力边界）"
        " → 治理五件事（执行记录 / 拒绝原因码 / 人工接管队列 / 审计链四元组哈希 / 改库篡改后再校验失败）"
        " → 收尾给系统五层架构（接入 / 认知 / 裁决 / 执行 / 账本 + 三条流 + 界面分层 + 代码层号）与自检数字")

EDITS = {
    "README.md": [
        ("4:38（278.52s）/ 1440×900", f"{mmss}（{sec}s）/ 1440×900"),
        ("演示与机制同步讲：系统五层架构总览（接入 / 认知 / 裁决 / 执行 / 账本 + 三条流 + 界面分层对照 + 代码层号 + 系统自检数字） → 认知层边界 → 三色闸门（R01/R06/R07/R09）→ 确认卡与能力令牌七步 → DAG 与两阶段资金 → 账单规则引擎 → 治理页落库形态 → 审计链四元组哈希与篡改实测 → 系统自检数字",
         f"演示按功能逐个真做一遍：{FLOW}"),
        ("（26 条旁白 / 59 段单行字幕，无重复）", f"（{cues} 条旁白 / {segs} 段单行字幕，无重复）"),
    ],
    "docs/提交物对照表.md": [
        ("4:38 / 278.52s / 1440×900 / 26 条旁白 / 59 段单行字幕无重复，旁白逐段讲机制",
         f"{mmss} / {sec}s / 1440×900 / {cues} 条旁白 / {segs} 段单行字幕无重复，旁白逐段讲功能怎么用"),
        ("4:38（278.52s），1440×900，h264+aac，25,442,258 B；26 条旁白 / 59 段单行字幕无重复。覆盖：系统五层架构总览（接入 / 认知 / 裁决 / 执行 / 账本 + 三条流 + 界面分层对照 + 代码层号 + 系统自检数字） → 开场先讲认知层（模型只提申请，不碰余额、不写库、不调工具）→ 绿灯 R01 直放 → 黄灯转账（同名消歧 + DAG 三节点 + 确认卡即授权凭证）→ 红灯挂失（R09 三因子 + 能力令牌七步 + 两阶段资金）→ 账单规则引擎与误报可能 → 业务管理订阅推断 → 计划/提醒 ticker → 治理页 DAG 落库、拒绝原因码与接管单来由 → 审计链四元组哈希校验通过 → 改库后再校验失败 → 系统自检（1857 测试 / 43 条红队 / 13 步 e2e / 37 张表）",
         f"{mmss}（{sec}s），1440×900，h264+aac，{size:,} B；{cues} 条旁白 / {segs} 段单行字幕无重复。覆盖：{FLOW}"),
        ("4:38 / 278.52s / 1440×900 / 26 条旁白 / 59 段单行字幕无重复",
         f"{mmss} / {sec}s / 1440×900 / {cues} 条旁白 / {segs} 段单行字幕无重复"),
    ],
    "docs/交付/比赛演示方案.md": [
        ("4:38（278.52s），1440×900，字幕 `演示录屏_字幕.srt` 59 段单行、无重复 | 覆盖架构总览（五层 + 三条流 + 界面分层 + 系统自检数字）→ 开场 → 绿灯查账单 → 黄灯转账（同名消歧 + 确认）→ 红灯挂失（三因子）→ 权限摘要 → 总览/账单/已核实/业务管理 → 计划/提醒 → 治理/接管队列 → 审计链校验通过 → 改库后再校验失败 → 收尾；重录脚本已入库：`scripts/demo_video/`（`record_final.py` 一次连续录屏、`mux_final.py` 合流、`mux_subs3.py` 单行字幕烧制、`story2.py` 旁白台本、`arch_layers.html` 架构讲解页，见该目录 README）",
         f"{mmss}（{sec}s），1440×900，字幕 `演示录屏_字幕.srt` {segs} 段单行、无重复 | 覆盖：{FLOW}；重录脚本已入库：`scripts/demo_video/`（`record_v7.py` 一次连续录屏、`apply_scale.py` 全片统一变速、`mux_final.py` 合流、`mux_subs3.py` 单行字幕烧制、`qa_video.py` 成片质检、`story2.py` 旁白台本、`arch_layers.html` 架构讲解页，见该目录 README）"),
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
