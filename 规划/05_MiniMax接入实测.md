# MiniMax 接入实测（2026-10-18 已验证跑通）

> 本文所有结论都来自本机对 `api.minimaxi.com` 的真实请求，不是文档抄录。复现方式见 `banking-agent/app/smoke.py`。

## 1. 凭据与地址（已定位）

| 项 | 值 |
|---|---|
| 变量名 | `MINIMAX_CN_API_KEY` |
| 存放位置 | `/home/peng/.dsh/.env`（另有副本 `/home/peng/.mmx/config.json` 的 `api_key`，`region: cn`） |
| 前缀 | `sk-cp-…`（cp = coding plan） |
| 区域 | 国内站 |
| API HOST | `https://api.minimaxi.com`（环境变量 `MINIMAX_API_HOST` 同值） |

注意：`.dsh/.env` 里还有邮箱密码等无关凭据，业务仓库**只读 MINIMAX 两个变量**，不要 source 整个文件。

## 2. 四个端点全部 200（同一把 key）

| 端点 | 协议形态 | 实测 | 结论 |
|---|---|---|---|
| `GET /v1/models` | OpenAI | 200 | 可枚举模型 |
| `POST /v1/chat/completions` | OpenAI 兼容 | 200 | **选它作为主通道** |
| `POST /v1/text/chatcompletion_v2` | MiniMax 自有 | 200 | 思维链走 `reasoning_content` 字段，想干净分离思考可用 |
| `POST /anthropic/v1/messages` | Anthropic 兼容 | 200 | 备用，返回 thinking block 带 signature |

## 3. 可用模型清单（`/v1/models` 实测）

`MiniMax-M3.1-Flash-Preview`、`MiniMax-M3`、`MiniMax-M2.7`、`MiniMax-M2.7-highspeed`、`MiniMax-M2.5`、`MiniMax-M2.5-highspeed`、`MiniMax-M2.1` …

### 选型结论

| 角色 | 模型 | 依据 |
|---|---|---|
| 主脑 cloud | **`MiniMax-M3.1-Flash-Preview`** | 内容干净无 think 污染；`tool_calls` 结构规范无冗余 `index`；单次约 1.4s |
| 备脑 | `MiniMax-M2.1` | 必可用，但 content 里混 `<think>`，且 coding plan 配额下更易 429 |
| 快速分类 fast | `MiniMax-M2.5-highspeed` | 注入分类这种小任务用 highspeed |

## 4. 关键实测发现（这四条直接决定代码怎么写）

### 4.1 工具调用可用 ✅

两个模型都返回 `finish_reason: tool_calls`，参数是 JSON 字符串：

```json
{"name":"query_balance","arguments":"{\"card_tail\": \"1234\"}"}
```

→ 中文意图 + 工具选择 + 参数抽取，一次调用全中。**我们所有结构化输出都走工具调用，不走 JSON mode。**

### 4.2 `response_format: json_object` 不被遵守 ❌（重要）

请求带 `response_format:{"type":"json_object"}` 时，M2.1 只在 content 里吐了一段 `<think>`，`finish_reason` 变成 `length`，**没有给出任何 JSON**。

→ 结论：**禁止依赖 JSON mode**。要结构化就定义成工具（哪怕是个假工具 `emit_result`），从 `tool_calls` 里取。这条如果不知道，会在写意图解析时浪费半天。

### 4.3 M2.1 的思维链混在 content 里 ⚠️

```
content: <think>\n用户想查余额…\n</think>\n\nok
```

→ 适配器必须无条件剥离 `<think>...</think>`，并把剥出来的内容放进 `reasoning` 字段。M3.1-Flash-Preview 不吐 think，但代码不能因此偷懒。

### 4.4 流式 SSE 可用，但**没有 `[DONE]` 结束符** ⚠️

流式返回 `data: {...}` 若干帧，最后是带 `finish_reason` 的那一帧，**没有** `data: [DONE]`。

→ 终端判定只能靠 `finish_reason` 非空，另加空闲超时兜底。如果照抄 OpenAI SDK 的 `while line != [DONE]` 写法会一直挂到超时。

## 5. 限流实测

- 响应头**没有** `x-ratelimit-*` 系列字段，拿不到配额剩余，只能靠 429 和延迟感知。
- 连发 5 次全部 200，耗时 1.35 / 1.64 / 1.72 / 1.47 / 1.41 秒，没有触发限流。
- 但 coding plan 名义配额约 40 次/5 小时。**20 条红队 + 回归测试会打穿**，所以适配器必须有：429 退避重试、请求计数、以及一个 `MOCK_MODE` 开关（不联网也能演）。

## 6. 对架构的三处修正

1. `LLMAdapter` 只暴露 `chat()` / `chat_stream()`，内部统一处理：剥 think、解析 tool_calls、429 退避、超时。上层业务代码永远看不到 `<think>`。
2. 意图解析的返回协议固定为一个名为 `emit_intent` 的工具，schema 里写死字段，模型没调用该工具就判定解析失败并重问一次（不崩、不猜）。
3. `/v1/text/chatcompletion_v2` 留作调试通道：它把 `reasoning_content` 单独给，答辩讲「模型在想什么」时可以直接展示，不影响主链路。

## 7. 环境变量（写进 `banking-agent/.env.example`）

```bash
MINIMAX_API_KEY=sk-cp-你的key
MINIMAX_API_HOST=https://api.minimaxi.com
MODEL_BRAIN=MiniMax-M3.1-Flash-Preview
MODEL_FAST=MiniMax-M2.5-highspeed
MODEL_BACKUP=MiniMax-M2.1
LLM_TEMPERATURE=0
LLM_TIMEOUT=60
MOCK_MODE=false
```

## 8. 稳定性与延迟实测（直接影响安全叙事）

同一 prompt、`temperature=0`、同一套工具，每个模型连跑 5 次：

| 模型 | 调对工具 | 参数正确 | 未调工具 | 单次延迟 |
|---|---|---|---|---|
| `MiniMax-M3.1-Flash-Preview` | 5/5 | 5/5 | 0 | 1.56s |
| `MiniMax-M3` | 1/1 | 1/1 | 0 | 0.92s |
| `MiniMax-M2.7` | 1/1 | 1/1 | 0 | 3.58s |
| `MiniMax-M2.5-highspeed` | 1/1 | 1/1 | 0 | 1.74s |
| `MiniMax-M2.1` | 5/5 | 5/5 | 0 | 1.87s |

### 但把首轮也算进去：M2.1 在 11 次调用里拒答了 1 次

原话是：`您好！我作为AI助手，无法直接访问银行系统或查询个人账户信息。`——它没有调工具，而是转成了安全拒答。

三条结论：

1. **`temperature=0` 不等于确定性。** 同一个问题，一次正常调工具，一次拒答。MoE 路由本身有随机性。
2. **意图解析必须有降级分支**：没拿到约定的工具调用 → 先重试一次 → 仍失败则转成澄清提问（话术：抱歉我没太理解，您是想查询余额吗），**绝不允许抛异常给用户看**。这条同时是完成率分和体验分的拿分点。
3. **答辩多一句硬话**：连模型自己都不稳定、会随手拒答，把资金安全寄托在 prompt 约束上等同于赌博；所以闸门必须是模型之外的确定性代码。这正好是我们整个方案的地基。

### 延迟结论

- 主链路一次工具调用 1.5—2.1 秒，多步 DAG（4 步）约 8 秒。演示时用流式把每一拍打出来，观众感知的是「正在思考」而不是「卡住了」。
- `M2.7` 明显更慢（3.6s），不适合做主脑。

