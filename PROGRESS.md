# 进度流水

> 接手的人（或模型）**第一件事就是读最后一段**，看清从哪继续。
> 格式固定：里程碑 / 日期 / 已完成 / 测试数据 / 遗留。

## m0 骨架  2026-10-18

- [x] 仓库结构、docs/ 00—10 交接规格书
- [x] `.env` 抽取脚本（setup_env.sh）、`.env.example`、`.gitignore`
- [x] `app/config.py`（Settings / load_env / endpoint）
- [x] `app/llm.py`（LLMAdapter / MockAdapter / 重试退避 / 思维链剥离 / 流式）
- [x] `app/smoke.py` 4 项自检
- 测试：smoke 4/4 PASS（工具调用 / think 剥离 / 流式收尾 / mock 兜底）
- 模型实测：主脑 MiniMax-M3.1-Flash-Preview 1.56s 稳定 5/5；M2.1 偶发拒答需重试
- 遗留：`app/main.py`、`web/index.html` 尚未创建，`make run` 还不能起

## 下一步

按 `docs/02_目录结构与文件职责.md` 的待建清单，从 m1 数据层开始：

1. `app/clock.py` 的异常层级（docs/04 章 4.1）
2. `app/money.py` 的中文金额解析（docs/04 章 4.3）
3. `app/models.py` 的 pydantic 模型（docs/04 章 4.5）
4. `app/db.py` 建表（docs/03 全部 DDL）
5. `app/audit.py` 哈希链（docs/04 章 4.7）
6. `scripts/seed.py` 种子数据（docs/03 章 3.x）
7. 补 `tests/test_audit.py` 篡改检测用例

完成后打 tag m1，回填本文件。
