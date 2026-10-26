# 用户视角功能体检 · 可跑脚本

配合 `docs/audit/用户视角功能体检_报告.md` 使用。这些脚本就是报告里"运行"证据的来源，均可复跑。

## 0. 会动数据，先备份

脚本会**真的写库**（建储蓄目标、建计划/提醒、造 handoff、走真实转账以触发红灯）。**不要对演示库跑**：

```bash
cd banking-agent
cp data/bank.db tmp/bank.backup-\$(date +%Y%m%d-%H%M%S).db
```

跑完还原：

```bash
cp tmp/bank.backup-<时间戳>.db data/bank.db && rm -f data/bank.db-wal data/bank.db-shm
```

## 1. 起一个独立实例（8123）

```bash
cd banking-agent
CONFIRM_TIMEOUT_S=120 SCHEDULED_TICK_MS=0 FACTOR_DEMO_ECHO=1 \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8123
```

- `SCHEDULED_TICK_MS=0`：关掉后台 ticker，避免提醒/计划在体检中途被自动推进。
- `CONFIRM_TIMEOUT_S=120`：确认卡 2 分钟过期，够跑强验证流程（默认也是 120s）。
- `FACTOR_DEMO_ECHO=1`：挑战响应里回显演示码，脚本才能自动完成双因子。
- 浏览器打开 `http://127.0.0.1:8123/#token=<data/api_token 的内容>`（**必须 #，不能 ?**）。

## 2. 脚本清单

| 脚本 | 覆盖 | 预期 |
|---|---|---|
| `probe_write.py` | 手动记一笔 / 提醒 / 计划 / 储蓄目标 / 适当性问卷 / 订阅定性 的边界入参 | 20+ 条 400，正常 goal 200 |
| `probe_birthday.py` | 生日计划：预算 0/负、生日格式/已过/2-30、项目 5 个、金额 0、标题 61 字、商户白名单 | 全 400（文案各异），无项目版 200 |
| `probe_remind2.py` | 提醒的"只管钱相关"领域闸门 + 过去时间 | 非钱相关 400 带示例文案；过去时间 200 |
| `probe_plan.py` | 计划 kind/payload 契约、13 步上限、到期滚动 | 400 + 列出各计划 due_ms 与状态（看"过去时间被 completed"） |
| `probe_factor.py` | 强验证 5 次锁定 → 重新签发挑战 → 绕过（P0/A1）；过期挑战与过期确认 | 锁定 200 TOO_MANY_ATTEMPTS 后，新挑战 attempts_left 回 5，正确码通过 |
| `q_overview.py` | 总览契约：handoffs/quick_actions/plans 的字段形状 | 打印 JSON 片段，核对计数与 actions |

`bx.py` 是共用客户端（SSE 聊天、确认、余额），Token 自动在上级目录找 `data/api_token`，也可用 `BANK_AUDIT_TOKEN` / `BANK_AUDIT_BASE` 覆盖。

## 3. 跑法

```bash
cd banking-agent
PYTHONPATH=docs/audit/scripts .venv/bin/python docs/audit/scripts/probe_write.py
PYTHONPATH=docs/audit/scripts .venv/bin/python docs/audit/scripts/probe_birthday.py
PYTHONPATH=docs/audit/scripts .venv/bin/python docs/audit/scripts/probe_remind2.py
PYTHONPATH=docs/audit/scripts .venv/bin/python docs/audit/scripts/probe_plan.py
PYTHONPATH=docs/audit/scripts .venv/bin/python docs/audit/scripts/probe_factor.py
```

## 4. 注意

- `probe_factor.py` 需要能跑通对话（mock 或真模型皆可）。走真模型会消耗配额；建议先用 mock（不填 `LLM_API_KEY` 时项目默认 mock 模式，具体看 `.env.example`）。
- 所有 `400` 都是预期结果，脚本不 assert，靠人读输出——它服务于"看见现象"，不是回归测试。真正的回归在 `tests/`（`make test`）。
- 不要改 `app/` 下任何代码来跑这些脚本；它们只发 HTTP。
