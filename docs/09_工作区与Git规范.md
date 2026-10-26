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
├── web/                  # 前端：index.html 壳 + app.css + js/ 17 个模块脚本
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
| `m1` | 数据层 | 35 张表 + 种子 + audit 哈希链单测通过 |
| `m2` | 工具层 | 14 工具全实现，纯函数单测通过（后续扩到 19：卡片五件套） |
| `m3` | 安全层 | 12 条规则 + token 七步 + 两阶段资金，单测覆盖 100%（历史基线；现为 13 条 R01–R13，见 `app/policy/rules.py`） |
| `m4` | 规划执行 | intent/DAG/executor 跑通转账全链路 |
| `m5` | API | 10 端点 + SSE 全事件（历史基线；现为 39 path / 44 operation，含 business 与 handoffs） |
| `m6` | 前端 | 双栏 + 决策剧场实时流 |
| `m7` | 红队 | 43 条红队用例：PASS 43 / FAIL 0（2026-10-23 实跑 **43/43 全过**；历史 14 条 FAIL 的逐条归因、修了哪些夹具/口径、产品侧补了什么，见 `docs/红队失败归因.md`） |
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
- [x] 13 条规则 engine.decide（R01–R13，求值顺序见 `app/policy/rules.py:RULES`）
- [x] token 签发与七步校验
- [x] 两阶段资金 hold/commit/release
- 测试：pytest 62 passed，policy 覆盖率 100%
- 红队：A/B 类 18 条全拦
- 遗留：C05 越权访问他人账户待 m5 补
```

接手的人**第一件事就是读 PROGRESS.md 最后一段**，看清从哪继续。

## 9.9 环境变量（`.env` 键名固定）

| 键 | 说明 | 示例 | 旧名（仍生效） |
|---|---|---|---|
| `LLM_API_KEY` | 运行时模型密钥，由 `setup_env.sh` 从 `~/.dsh/.env` 抽取（或 `--prompt` 交互粘贴） | `sk-...` | `MINIMAX_API_KEY` |
| `LLM_BASE_URL` | OpenAI 兼容网关 base url（末尾带 `/v1`、`/v4` 时只补 `/chat/completions`，否则补 `/v1/chat/completions`） | `https://api.minimaxi.com` | `MINIMAX_API_HOST` |
| `LLM_ENDPOINT` | 直接指定完整 chat/completions URL，覆盖 base 拼接规则（可选） | `https://open.bigmodel.cn/api/paas/v4/chat/completions` | — |
| `LLM_MODEL` | 主脑 | `MiniMax-M3.1-Flash-Preview` | `MODEL_BRAIN` |
| `LLM_MODEL_FAST` | 快速分类 | `MiniMax-M2.5-highspeed` | `MODEL_FAST` |
| `LLM_MODEL_BACKUP` | 备脑，**只给 `python3.12 -m app.smoke` 的思维链剥离自检用，生产链路不读、也不做自动降级** | `MiniMax-M2.5` | `MODEL_BACKUP` |
| `LLM_TEMPERATURE` | 固定 0 | `0` | — |
| `LLM_TIMEOUT` | 单次调用超时（秒） | `60` | `LLM_TIMEOUT_S` |
| `SCHEDULED_TICK_MS` | 定时转账后台节拍间隔（毫秒），`0` 关闭后台节拍 | `30000` | — |
| `MOCK_MODE` | 真值（`1`/`true`/`yes`/`on`）= 对抗恶意桩；`script` = 离线协作桩；`false` = 真模型 | `false` | — |
| `CAPABILITY_SECRET` | 能力令牌 HMAC 密钥（64 位十六进制）。**未设置**时首次运行自动生成并落盘到 `data/token_secret`（0600，gitignored），重启复用；**设置了**（含空串）就只认 64 位十六进制，不合格则黄/红灯全链路 fail closed | 自动生成 `token_hex(32)` | — |
| `CAPABILITY_SECRET_FILE` | 自动生成密钥的落盘路径 | `data/token_secret` | — |
| `CONFIRM_TIMEOUT_S` | 确认卡等待窗口（秒），范围 10—3600，非法值回落 120 并打 `planner.confirm_timeout.*` 警告；SSE 的 `expires_in_s` 与确认卡 `expires_ms` 同档 | `120` | — |

> 数据库路径**不是**环境变量：固定为 `data/bank.db`（`app/db.py:21 DEFAULT_DB_PATH`）；测试用 `monkeypatch.setattr(db, "DEFAULT_DB_PATH", ...)` 换库。
> 以上键名与 `app/config.py` 的 `Settings` 字段、`app/security/token.py:32` 逐字对齐。
> **新名优先、旧名兜底**：`LLM_*` 是通用名（任意 OpenAI 兼容厂商），旧的 `MINIMAX_*/MODEL_*` 仍生效，老 `.env` 不用重写。
> 此前文档里的错名 `MINIMAX_CN_API_KEY`/`DB_PATH`/`TOKEN_SECRET` 依旧不生效（照抄会不工作）。
> **换厂商怎么配、各家填法、报错速查**：见 [模型配置说明](模型配置说明.md)。

## 9.10 工作区卫生

| 规则 | 说明 |
|---|---|
| `data/bank.db` 可随时删除重建 | `make seed` 一条命令恢复 |
| 演示前必跑 `POST /api/demo/reset` | 保证数据回到初始态 |
| 不要把下载的模型/截图放仓库 | 放 `tmp/` 或仓库外 |
| 每完成一天工作 `git status` 必须干净 | 脏工作区是接手者的噩梦 |

## 9.11 解释器与测试约定（2026-10-18 实测补充）

| 事实 | 结论 |
|---|---|
| 本机 /usr/bin/python3 是 3.14 | 不能用 |
| 本机 python3.11 无第三方包 | 不做首选 |
| 本机 python3.12（/home/peng/.local/bin/python3.12）已有 fastapi 0.136.3、pydantic 2.13.4、httpx 0.28.1、pyyaml、pytest 9.0.3 | 项目统一用它 |
| 真正原因是本机 pip.conf 里的 `extra-index-url=https://pypi.ngc.nvidia.com` 解析不了；`make setup` / `make venv` 用 `PIP_CONFIG_FILE=/dev/null` + `pip --isolated` 绕开即可建 venv | 两条路都留：venv（`make setup`）或系统 3.12 |
| pytest-asyncio、pytest-cov 装不上 | 异步测试用 asyncio.run() 包一层；覆盖率改用本文 10 章的分支清单自证 |

| 约定 | 命令 |
|---|---|
| 一键配置 | bash scripts/setup.sh（或 make setup；`--dry-run` 预演，`--ollama`/`--mock` 点名模型来源） |
| 跑测试 | python3.12 -m pytest -q tests/ |
| 起服务 | python3.12 -m uvicorn app.main:app --reload --port 8000 |
| 种子 | python3.12 -m scripts.seed（已有数据会跳过；恢复初始态用 --force 删库重建，等价 make reseed） |
| 红队 | python3.12 -m scripts.redteam |
| 模型自检 | python3.12 -m app.smoke |

异步测试的统一写法（不用 pytest-asyncio）：

    def test_executor_runs():
        import asyncio
        from app.planner.executor import run
        asyncio.run(run(session_id='s1', dag=my_dag, emit=lambda *a: None))

## 9.12 推送通道与镜像失效的绕行配方（2026-10-22 实测）

本机全局 git 启用了镜像重写：`url.https://ghfast.top/https://github.com/.insteadof=https://github.com/`。
这带来两个问题，都不是仓库本身的毛病：

| 现象 | 根因 |
|---|---|
| `fatal: could not read Username for 'https://ghfast.top'` | 凭据 helper 只挂在 github.com 域（`credential.https://github.com.helper=!/usr/bin/gh auth git-credential`），镜像域拿不到凭据 |
| `Recv failure: 连接被对方重置` | ghfast.top 镜像本身不可用（2026-10-22 实测挂掉，`git ls-remote`/`git push` 全失败） |

### 9.12.1 已验证可用的推送配方

用隔离 HOME + 内联 gh token + 把 mirror 映射反掉（等于禁用镜像直连 github.com）：

```bash
TMPH=$(mktemp -d)
printf '[credential "https://github.com"]\n\thelper = "!f() { echo username=x-access-token; echo password=$(gh auth token); }; f"\n' > $TMPH/.gitconfig
HOME=$TMPH GIT_CONFIG_GLOBAL=$TMPH/.gitconfig \
  git -c "url.https://github.com/.insteadof=https://ghfast.top/https://github.com/" \
  push origin main
rm -rf $TMPH
```

要点三条：

1. `HOME` 必须隔离——否则全局 credential helper 仍指向镜像域。
2. `GIT_CONFIG_GLOBAL` 要跟着 HOME 走，inline token 才生效。
3. `-c url.https://github.com/.insteadof=...` 把镜像规则反向覆盖，让地址变回 github.com 直连。

### 9.12.2 只读核对远端（不依赖 git 传输）

git 通道挂掉时，远端状态改用 GitHub API 查：

```bash
gh api repos/peng6yu/banking-agent --jq '.private, .size, .pushed_at, .default_branch'
```

### 9.12.3 交接注意

- **提交前必须确认仓库可见性**：`gh repo view --json visibility`。赛题要求交仓库地址，`PRIVATE` 评委打不开，等于没交。
- 改 public 前先扫密钥：`git grep -n 'sk-cp-'` 只应命中占位符（如 `sk-cp-...`）。`.env` 已在 `.gitignore` 内。
- 若 git 两种通道都不可用，最后手段是 GitHub API 写文件（`gh api --method PUT /repos/{owner}/{repo}/contents/{path}`），但会丢失多文件的单次原子提交语义，仅作应急。
