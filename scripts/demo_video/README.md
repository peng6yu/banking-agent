# 演示视频流水线（可复现）

这一目录是 `docs/交付/演示录屏_全功能.mp4` 的完整生成脚本，交付视频（4:42 / 282.88s / 1440×900 / 43 条旁白 / 52 段单行字幕）由这里的脚本一次跑出来。

## 为什么在这里（而不是 tmp/）

上一版把脚本放在 `tmp/`，而 `tmp/` 在 `.gitignore` 里，评审拿到仓库后没法重录。本目录是入库版本，路径已改成从仓库根算起（`Path(__file__).resolve().parents[2]`）。

## 目录内容

| 文件 | 作用 |
|---|---|
| `story2.py` | 旁白台本（43 条，键名 + 文案）。改文案后必须删掉 `tmp/voice4/` 里同名键的 mp3，否则会命中旧音频 |
| `synth_edge.py` | 用 edge-tts（`zh-CN-YunxiNeural`，+10%）逐条合成旁白，产出 `tmp/voice4/*.mp3` 与 `meta.json`；单次成功率低，脚本内部重试到成功并校验「字/秒」区间 |
| `arch_layers.html` | 架构讲解页（五层 / 三条流 / 界面分层对照 / 代码层号 L5→L0 / 系统自检数字），录屏时以 `file://` 打开 |
| `record_v7.py` | v7 录屏：43 条 cue，每一条都做真实操作（建计划、填表、点裁决、跑监测……）。起 demo 库 `tmp/final7.db`、起 uvicorn（`PORT=8470`、`API_TOKEN=rec`）、逐页断言无 401，产出 `tmp/finalvideo7/*.webm` 与带时间轴的 `tmp/cues.json` |
| `apply_scale.py` | 给整片打统一变速系数（写进 `tmp/cues.json` 的 `scale`），画面/旁白/字幕一起缩放 |
| `speed_voice.py` | 把 43 条旁白逐条 `atempo` 加速到 `tmp/voice5/`，并写 `tmp/voice5/meta.json` |
| `mux_final.py` | webm → mp4（`setpts` 变速），旁白按每条 cue 起点 `adelay` 铺到一条音轨，产出 `tmp/final_silent.mp4`、`tmp/final_audio.m4a`、`tmp/final.srt` |
| `mux_subs3.py` | 用 drawtext 烧单行字幕（长句按标点比例切段，画面永远一行），产出 `tmp/final_voice_subs.mp4` |
| `qa_video.py` | 成片质检：时长/分辨率/码率、字幕重叠与重复、画面里有没有红色报错条/黄色警告条、字幕有没有叠成两行 |
| `update_docs_video.py` | 把成片真实时长/字节/条数写回 `README.md`、`docs/提交物对照表.md`、`docs/交付/比赛演示方案.md` |

## 重录步骤（仓库根执行）

```bash
.venv/bin/python scripts/demo_video/synth_edge.py                  # 1. 合成旁白 -> tmp/voice4/
/home/peng/.local/bin/python3.12 scripts/demo_video/record_v7.py   # 2. 一次连续录屏 -> tmp/finalvideo7/*.webm + tmp/cues.json
.venv/bin/python scripts/demo_video/apply_scale.py 1.14           # 3. 打统一变速系数（成片要 <= 300s）
.venv/bin/python scripts/demo_video/speed_voice.py                 # 4. 旁白同样变速 -> tmp/voice5/
.venv/bin/python scripts/demo_video/mux_final.py                   # 5. 画面+旁白+字幕文件 -> tmp/final_silent.mp4 tmp/final_audio.m4a tmp/final.srt
.venv/bin/python scripts/demo_video/mux_subs3.py                   # 6. 烧单行字幕 -> tmp/final_voice_subs.mp4
cp tmp/final_voice_subs.mp4 'docs/交付/演示录屏_全功能.mp4'
cp tmp/final.srt 'docs/交付/演示录屏_字幕.srt'
.venv/bin/python scripts/demo_video/qa_video.py                    # 7. 质检（时长 / 字幕 / 画面）
.venv/bin/python scripts/demo_video/update_docs_video.py           # 8. 回填文档数字
.venv/bin/python scripts/check_docs_numbers.py                     # 9. 数字一致性自检
```

## 为什么整片统一变速（而不是砍内容）

`docs/提交物对照表.md` 要求「演示视频（≤5 分钟）」。这版要讲 **43 个功能点**，一次连续录下来是 322s，超了 22s。砍旁白省不动——43 个操作动作本身的耗时 ≈ Σ 旁白时长 ≈ 264s，画面已经录好，动作占的时间删不掉。所以选择对整片（画面 + 旁白 + 字幕）统一加速 1.14 倍：内容一条不删，时长精确落到 282.88s，三者仍然严格对齐。

**踩过的坑**：在 ffmpeg 的 `filter_complex` 里直接写 `atempo=1.14` 并和 43 路旁白一起混音，输出会是空音轨（`duration=N/A`、文件只有 1KB 出头）。正确做法是先逐条 `atempo` 落盘（`speed_voice.py`），混音图里只保留 `adelay`。

## 这版修掉的三个老毛病

1. **页签打不开（401）**：录屏时服务端必须用 `API_TOKEN=rec` 启动，页面用 `http://127.0.0.1:8470/#token=rec` 打开；前端会把 hash 里的令牌写进 sessionStorage 再抹掉 hash。令牌不匹配时，账务/计划/治理三页每张卡都会显示「这块没加载出来」，对话页顶部会挂黄色告警条。`record_v7.py` 起服务后逐页断言没有「没加载出来 / 接口鉴权没通过 / 加载失败」再继续录。
2. **字幕重复两行**：老流水线是分段录制后拼接，`caps_all.json` 跨轮追加导致同一句 cue 出现两份（只差十几毫秒），字幕因此在画面上叠成两行黑底。现在改成**一次连续录完整条**，cue 与视频时间轴同源，天然不会重复；`mux_final.py` 生成的 SRT 还会检查无重叠、无重复文案、无换行。
3. **演示不够详细**：从 26 条旁白扩到 43 条，覆盖总览/账单/异常/流水/储蓄计划/目标/生日/提醒/订阅/适当性/能力边界/记忆/执行记录/拒绝/接管/审计链/架构/自检等全部功能点，每条旁白都对应一次真实操作。

## 注意

- 录屏依赖 Playwright（`python3.12` 环境已装）与 `ffmpeg`/`ffprobe`；`.venv` 里没有 Playwright，跑录屏脚本要用 `/home/peng/.local/bin/python3.12`。
- `record_v7.py` 会自己起 uvicorn（`PORT=8470`）并用 `tmp/final7.db`（由 `scripts/make_demo_db.py` 重新生成），**不会**碰 `data/bank.db`。
- 录屏画面与旁白之间有约 1.8 秒的 webm 预卷偏移，`record_v7.py` 会把偏移写进 `tmp/cues.json` 的 `shift`，合流时统一补偿，抽帧 QA 要按同一偏移和同一 `scale` 取帧。
- `qa_video.py` 的红色/黄色像素统计只是**线索**（卡片底色也会命中），报错条/警告条最终要人看 `tmp/qa/sheet_*.jpg` 四张联系表确认；字幕「叠成两行」是机器可判的硬指标。
- 演示库的丰富数据由 `scripts/make_demo_db.py` 负责（多目标 / 多计划 / 多提醒 / 接管单 / 拒绝记录 / 异常核实 / 适当性 / 订阅裁决 / 储蓄扣款历史）。
