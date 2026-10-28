# 演示视频流水线（可复现，v8）

这一目录是 `docs/交付/演示录屏_全功能.mp4` 的完整生成脚本。交付视频（**4:44 / 284.52s / 1440×900 / 43 条旁白 / 57 段单行字幕 / 33,835,173 B**）由这里的脚本一次跑出来。

## 为什么在这里（而不是 tmp/）

更早一版把脚本放在 `tmp/`，而 `tmp/` 在 `.gitignore` 里，评审拿到仓库后没法重录。本目录是入库版本，路径统一从仓库根算起（`Path(__file__).resolve().parents[2]`）。

## 目录内容

| 文件 | 作用 |
|---|---|
| `story_v8.py` | v8 旁白台本（43 条，键名 + 文案 + 对应 cue 顺序）。改文案后必须删掉 `tmp/voice6/` 里同名键的 mp3，否则会命中旧音频 |
| `synth_edge_v8.py` | 用 edge-tts（`zh-CN-YunxiNeural`）逐条合成旁白到 `tmp/voice6/`，写 `tmp/voice6/meta.json`；文案没变的从 v7 的 `tmp/voice4/` 直接复用，只新合成改动的句子 |
| `arch_layers.html` | 架构讲解页（五层 / 三条流 / 界面分层对照 / 代码层号 L5→L0 / 系统自检数字），录屏时以 `file://` 打开 |
| `record_v8.py` | v8 录屏：43 条 cue，每一条都做真实操作（建储蓄目标、算每期缺口、填表建计划、点裁决、跑监测、篡改审计链……）。起 demo 库 `tmp/final8.db`、起 uvicorn（`PORT=8470`、`API_TOKEN=rec`）、逐页断言无 401，产出 `tmp/finalvideo8/*.webm` 与带时间轴的 `tmp/cues.json` |
| `apply_scale.py` | 给整片打统一变速系数（写进 `tmp/cues.json` 的 `scale`），画面 / 旁白 / 字幕一起缩放 |
| `speed_voice.py` | 把旁白逐条 `atempo` 加速落盘（`DSH_VOICE_SRC` → `DSH_VOICE_DST`），并写目标目录的 `meta.json` |
| `mux_final.py` | webm → mp4（`setpts` 变速），旁白按每条 cue 起点 `adelay` 铺到一条音轨，产出 `tmp/final_silent.mp4`、`tmp/final_audio.m4a`、`tmp/final.srt` |
| `mux_subs3.py` | 用 drawtext 烧单行字幕（长句按标点比例切段，画面永远一行）。**必须带 `--burn`**，否则只写 SRT 不重烧视频 |
| `qa_video.py` | 成片质检：时长 / 分辨率 / 码率、字幕重叠与重复文案、画面里有没有红色报错条 / 黄色警告条、字幕有没有叠成两行 |
| `update_docs_video.py` | 把成片真实时长 / 字节 / 条数写回 `README.md`、`docs/提交物对照表.md`、`docs/交付/比赛演示方案.md`（按旧串 / 正则替换，可重复运行） |

## 重录步骤（仓库根执行）

```bash
# 1. 合成旁白 -> tmp/voice6/
.venv/bin/python scripts/demo_video/synth_edge_v8.py

# 2. 一次连续录屏 -> tmp/finalvideo8/*.webm + tmp/cues.json
/home/peng/.local/bin/python3.12 scripts/demo_video/record_v8.py

# 3. 打统一变速系数（成片必须 <= 300s）
.venv/bin/python scripts/demo_video/apply_scale.py 1.17

# 4. 旁白同样变速 -> tmp/voice7/
DSH_VOICE_SRC=tmp/voice6 DSH_VOICE_DST=tmp/voice7 .venv/bin/python scripts/demo_video/speed_voice.py

# 5. 画面 + 旁白 + 字幕文件 -> tmp/final_silent.mp4 tmp/final_audio.m4a tmp/final.srt
DSH_VOICE_DST=tmp/voice7 .venv/bin/python scripts/demo_video/mux_final.py

# 6. 烧单行字幕 -> tmp/final_voice_subs.mp4
.venv/bin/python scripts/demo_video/mux_subs3.py --burn

# 7. 落盘 + 质检
cp tmp/final_voice_subs.mp4 'docs/交付/演示录屏_全功能.mp4'
cp tmp/final.srt 'docs/交付/演示录屏_字幕.srt'
.venv/bin/python scripts/demo_video/qa_video.py

# 8. 回填文档数字 + 数字一致性自检
.venv/bin/python scripts/demo_video/update_docs_video.py
.venv/bin/python scripts/check_docs_numbers.py
```

## 为什么整片统一变速（而不是砍内容）

`docs/提交物对照表.md` 要求「演示视频（≤5 分钟）」。v8 一次连续录下来是 332.8s，超了 32.8s。砍旁白省不动——43 个操作动作本身的耗时 ≈ Σ 旁白时长，**动作占的时间删不掉**。所以选择对整片（画面 + 旁白 + 字幕）统一加速 1.17 倍：内容一条不删，时长精确落到 284.52s，三者仍然严格对齐。

**踩过的坑**：在 ffmpeg 的 `filter_complex` 里直接写 `atempo=1.17` 并和 43 路旁白一起混音，输出会是空音轨（`duration=N/A`、文件只有 1KB 出头）。正确做法是先逐条 `atempo` 落盘（`speed_voice.py`），混音图里只保留 `adelay`。

## v8 相比 v7 的三处内容改动

1. **架构前置**：开头先讲系统五层架构（接入 / 认知 / 裁决 / 执行 / 账本 + 三条流 + 界面分层 + 代码层号 L5→L0），再用「系统自检数字」收尾，两处都对着 `arch_layers.html` 的真实 DOM 讲。
2. **计划段扩到十件事**：新增「跟助手说一句话建储蓄目标」与「助手算每月要存多少 / 还差多少」两条 cue（`a_goal` / `a_goal_calc`），并把「目标回计划页并能推进跨场景计划」改成走真实控件：`#btn-pf-refresh` 刷新后 `#pf-goal-list` 出现目标卡片，再点 `#plan-persist` 里的「推进这个计划」（这两个控件都是动态生成的，不在任何 `<summary>` 标签里）。
3. **治理段闭环**：执行记录 → 拒绝原因码 → 人工接管队列 → 审计链通过 → 改库篡改后再校验失败 → 证据链说明，收尾接系统自检数字。

## 这版修掉的三个老毛病

1. **页签打不开（401）**：录屏时服务端必须用 `API_TOKEN=rec` 启动，页面用 `http://127.0.0.1:8470/#token=rec` 打开；前端会把 hash 里的令牌写进 sessionStorage 再抹掉 hash。令牌不匹配时，账务 / 计划 / 治理三页每张卡都会显示「这块没加载出来」，对话页顶部会挂黄色告警条。`record_v8.py` 起服务后逐页断言没有「没加载出来 / 接口鉴权没通过 / 加载失败」再继续录。
2. **字幕重复两行**：老流水线是分段录制后拼接，同一句 cue 出现两份（只差十几毫秒），字幕因此在画面上叠成两行黑底。现在改成**一次连续录完整条**，cue 与视频时间轴同源，天然不会重复；`mux_final.py` 生成的 SRT 还会检查无重叠、无重复文案、无换行。
3. **演示不够详细**：43 条旁白逐个对应一次真实操作，覆盖总览 / 账单 / 异常 / 流水 / 手动记账 / 储蓄计划 / 储蓄目标 / 目标测算 / 跨场景计划 / 提醒 / 生日模板 / 模拟支出 / 监测告警 / 排期 / 卡片 / 订阅 / 适当性 / 记忆 / 能力边界 / 执行记录 / 拒绝码 / 接管 / 审计链 / 篡改失败 / 证据链 / 架构 / 自检。

## 注意

- 录屏依赖 Playwright（`python3.12` 环境已装）与 `ffmpeg` / `ffprobe`；`.venv` 里没有 Playwright，跑录屏脚本要用 `/home/peng/.local/bin/python3.12`。
- `record_v8.py` 会自己起 uvicorn（`PORT=8470`）并用 `tmp/final8.db`（由 `scripts/make_demo_db.py` 重新生成），**不会**碰 `data/bank.db`。
- 录屏画面与旁白之间有约 3.4 秒的 webm 预卷偏移，`record_v8.py` 会把偏移写进 `tmp/cues.json` 的 `shift`，合流时统一补偿，抽帧 QA 要按同一偏移和同一 `scale` 取帧。
- `qa_video.py` 的红色 / 黄色像素统计只是**线索**（卡片底色也会命中），报错条 / 警告条最终要人看 `tmp/qa/sheet_*.jpg` 四张联系表确认；字幕「叠成两行」是机器可判的硬指标。
- 演示库的丰富数据由 `scripts/make_demo_db.py` 负责（多目标 / 多计划 / 多提醒 / 接管单 / 拒绝记录 / 异常核实 / 适当性 / 订阅裁决 / 储蓄扣款历史）。