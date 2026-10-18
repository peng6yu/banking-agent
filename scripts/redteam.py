#!/usr/bin/env python3.12
"""m7 红队 runner：把 tests/redteam/cases.yaml 的 43 条攻击用例跑通整链路并自动判定。

用法：
    python3.12 -m scripts.redteam            # 跑全部 43 条
    python3.12 scripts/redteam.py --case D01 # 只跑某一条（调试用）
    python3.12 -m scripts.redteam --list     # 只列用例

行为严格按 docs/10 §10.5：
    1. 每条用例前重置 data/bank.db 并加载种子数据（互不污染）；
    2. 落 setup 的前置数据与故障钩子 → 走完整链路 intent → dag → executor → tools；
       默认不响应任何 confirm（不点确认、不给人脸与短信因子）；
    3. 对 expect 四个字段逐条断言：level（下限 green<yellow<red）、denied、
       must_not、must_contain_audit（docs/03 §3.2 闭集）；
    4. 打印七列表格：id / 类别 / 期望级别 / 实际级别 / 是否拦住 / 审计是否齐 / 结果；
    5. 任一条 FAIL 退出码 1，全 PASS 退出码 0。

模块分工（每个文件都控制在 400 行以内，docs/08 硬上限）：
    scripts/redteam.py        本文件：离线脚本模型 + 用例循环 + 故障注入 + 表格与退出码
    scripts/redteam_setup.py  setup 前置数据落库与账务快照
    scripts/redteam_judge.py  must_not 探针、直探针与四字段判定

离线保证：模型调用全部走本文件的 ScriptedAdapter，不 import 任何真实 LLM 客户端、
不读 API key，答辩现场断网也能跑完。脚本模型只还原「模型输出意图与槽位」这一步，
策略裁决、DAG、令牌、两阶段资金、工具执行全是生产代码。

诚实原则：用例集 tests/redteam/cases.yaml 是冻结交付物，本 runner 只读不写；
跑不过的用例如实写进 tmp/redteam_failures.txt，绝不为全绿删改或放宽任何断言。

已知的 runner 侧约定（不削弱断言，只影响前置数据的落法）：
    * setup.payees 按「确保存在」落库：同名同尾号已有就不重复插，
      否则 A04—A06 的三个同名张三会让后续判定失真；
    * E04 的 hold 由 runner 用生产 funds.initiate 建立（等价于用户点确认后执行器
      要做的那一步），确认环节本身仍不由 runner 代答。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app import audit, clock, db, funds  # noqa: E402
from app.api import routes  # noqa: E402
from app.clock import BankError  # noqa: E402
from app.ids import new_id  # noqa: E402
from app.llm import LLMError, LLMResponse, ToolCall  # noqa: E402
from app.money import parse_amount  # noqa: E402
from app.planner import dag as planner_dag  # noqa: E402
from app.planner import executor, intent  # noqa: E402
from scripts.redteam_judge import (  # noqa: E402
    Evidence,
    judge,
    run_forged_payee_probe,
    run_token_probes,
)
from scripts.redteam_setup import Applied, Snapshot, apply_setup, snapshot_of  # noqa: E402

CASES_PATH = ROOT / "tests" / "redteam" / "cases.yaml"
FAILURE_PATH = ROOT / "tmp" / "redteam_failures.txt"

#: 本 runner 自带的 64 位 hex 测试密钥：只在内存里，不写盘、不进 .env。
TEST_SECRET = "7f" * 32

#: 没有人工干预：把确认超时压到 80ms，等价于「没人点确认」，免得每条空等 120 秒。
CONFIRM_NO_HUMAN_MS = 80

LEVEL_ORDER: dict[str, int] = {"green": 0, "yellow": 1, "red": 2}
HARD_CAP_FEN = 5_000_000  # 规划/02 §2.3：5 万红线
