# 09 工作区与 Git 规范

## 9.1 仓库根目录（**只允许这些**）

```
banking-agent/
├── .env                  # 本地密钥，不进 git
├── .env.example          # 进 git
├── .gitignore
├── README.md
├── Makefile
├── requirements.txt
├── setup_env.sh
├── app/                  # 后端
├── web/                  # 前端单文件
├── tests/                # pytest
├── scripts/              # 种子、红队、演示脚本
├── data/                 # 运行时 sqlite，不进 git
├── docs/                 # 规格文档 00—10
├── 规划/                 # 前期选型与实测记录
└── PROGRESS.md           # 进度流水，进 git
```

**禁止**在根目录放：临时脚本、`test.py`、`a.txt`、截图、下载的模型、`node_modules`。临时文件一律放 `tmp/`（已在 `.gitignore`）。

## 9.2 `.gitignore`（逐条照抄）

```gitignore
.venv/
__pycache__/
*.pyc
.pytest_cache/
.ruff_cache/
.env
data/*.db
data/*.db-wal
data/*.db-shm
tmp/
*.log
.DS_Store
node_modules/
```

## 9.3 分支策略（单人 + 后续模型接手）

采用 **trunk-based**：

| 分支 | 用途 |
|---|---|
| `main` | 始终可运行、测试全绿 |
| `feat/mN-短描述` | 单个里程碑的开发分支，如 `feat/m3-policy` |
| `fix/短描述` | 修 bug |

规则：

| 规则 | 说明 |
|---|---|
| 一个分支只做一个里程碑 | 便于回滚 |
| 合并前必须 `pytest` 全绿 | 不绿不合并 |
| 合并用 `git merge --no-ff` | 保留里程碑边界 |
| 禁止在 `main` 上直接写代码 | 除 README/PROGRESS 这类文档提交 |
| 禁止 `git push --force` 到 `main` | 历史要留给评审看 |
| 里程碑完成打 tag | `m0` … `m8`，见 9.5 |

## 9.4 commit message 规范

格式：`<type>(<scope>): <一句话>`

| type | 用于 |
|---|---|
| `feat` | 新功能（对应里程碑任务） |
| `fix` | 修 bug |
| `test` | 只加/改测试 |
| `docs` | 只改文档 |
| `refactor` | 不改行为的重构 |
| `chore` | 依赖、脚本、配置 |

scope 取值：`db` `money` `audit` `policy` `token` `tools` `planner` `api` `web` `seed` `redteam` `docs`。

示例：

```
feat(policy): 实现 R06/R08 转账黄红线与日累计判定
fix(token): nonce 重放校验漏了 commit 前二次检查
test(redteam): 补 A07 备注注入转双因子用例
docs(05): 补充 hold 超时后的补偿路径
```

| 禁止 | 原因 |
|---|---|
| `update` / `fix bug` / `111` 这类 message | 无法追溯 |
| 一个 commit 混多件事 | 无法 review、无法回滚 |
| 提交 `.env` 或 `data/*.db` | 泄密 + 仓库膨胀 |

## 9.5 里程碑与 tag

| tag | 里程碑 | 完成标志 |
|---|---|---|
| `m0` | 骨架 | 目录、依赖、health 端点、CI 可跑 |
| `m1` | 数据层 | 15 张表 + 种子 + audit 哈希链单测通过 |
| `m2` | 工具层 | 10 工具全实现，纯函数单测通过 |
| `m3` | 安全层 | 11 条规则 + token 七步 + 两阶段资金，单测覆盖 100% |
| `m4` | 规划执行 | intent/DAG/executor 跑通转账全链路 |
| `m5` | API | 10 端点 + SSE 全事件 |
| `m6` | 前端 | 双栏 + 决策剧场实时流 |
| `m7` | 红队 | 43 条全拦，0 漏 |
| `m8` | 交付 | README/PPT/安全自评/演示脚本齐 |

打 tag 命令：`git tag -a m3 -m '安全层完成' && git push origin m3`

## 9.6 推到 GitHub 的流程

首次（已确认本机 `gh` 已登录账号 **peng6yu**）：

```bash
cd banking-agent
git init -b main
git add -A
git commit -m 'chore: 初始化仓库与交接文档'
gh repo create banking-agent --private --source=. --remote=origin --push
```

日常：

```bash
git checkout -b feat/m3-policy
# ...开发...
python -m pytest -q && python -m scripts.redteam
git add -A && git commit -m 'feat(policy): ...'
git push -u origin feat/m3-policy
git checkout main && git merge --no-ff feat/m3-policy && git push
```

| 事项 | 规定 |
|---|---|
| 仓库可见性 | **private**（含种子用户数据与安全设计，赛前不公开） |
| 提交截止前 | 转为 public 或给评审只读链接（赛题要求可访问的仓库） |
| 密钥 | `.env` 永不入库；`.env.example` 只放键名不放值 |
| 提交前检查 | `git status` 里不能出现 `.env`、`*.db` |

## 9.7 回滚

| 场景 | 做法 |
|---|---|
| 刚提交未推 | `git reset --soft HEAD~1` |
| 已推送到 feature 分支 | `git revert <sha>` |
| 已推送到 main | **一律 `git revert`**，禁止 `reset --hard` |
| 误提交了 `.env` | 立刻吊销该 key，`git rm --cached .env`，然后重写历史并强推 feature 分支（main 不允许） |

## 9.8 PROGRESS.md 维护规则

每完成一个里程碑追加一段，格式固定：

```
## m3 安全层  2026-10-25
- [x] 11 条规则 engine.decide
- [x] token 签发与七步校验
- [x] 两阶段资金 hold/commit/release
- 测试：pytest 62 passed，policy 覆盖率 100%
- 红队：A/B 类 18 条全拦
- 遗留：C05 越权访问他人账户待 m5 补
```

接手的人**第一件事就是读 PROGRESS.md 最后一段**，看清从哪继续。

## 9.9 环境变量（`.env` 键名固定）

| 键 | 说明 | 示例 |
|---|---|---|
| `MINIMAX_CN_API_KEY` | 运行时模型密钥，从 `/home/peng/.dsh/.env` 取 | `sk-cp-...` |
| `MINIMAX_API_HOST` | 固定 | `https://api.minimaxi.com` |
| `LLM_MODEL_BRAIN` | 主脑 | `MiniMax-M3.1-Flash-Preview` |
| `LLM_MODEL_FAST` | 快速分类 | `MiniMax-M2.5-highspeed` |
| `LLM_MODEL_BACKUP` | 备脑 | `MiniMax-M2.1` |
| `LLM_TEMPERATURE` | 固定 0 | `0` |
| `LLM_TIMEOUT_S` | 单次调用超时 | `60` |
| `MOCK_MODE` | 1 = 全 mock 不起网络 | `0` |
| `DB_PATH` | sqlite 路径 | `data/bank.db` |
| `TOKEN_SECRET` | HMAC 密钥，**首次启动自动生成到 data/ 并忽略进 git** | — |

## 9.10 工作区卫生

| 规则 | 说明 |
|---|---|
| `data/bank.db` 可随时删除重建 | `make seed` 一条命令恢复 |
| 演示前必跑 `POST /api/demo/reset` | 保证数据回到初始态 |
| 不要把下载的模型/截图放仓库 | 放 `tmp/` 或仓库外 |
| 每完成一天工作 `git status` 必须干净 | 脏工作区是接手者的噩梦 |
