# 演示视频流水线（可复现）

这一目录是 `docs/交付/演示录屏_全功能.mp4` 的完整生成脚本，交付视频（4:38 / 278.52s / 1440×900 / 26 条旁白 / 59 段单行字幕）由这里的脚本一次跑出来。

## 为什么在这里（而不是 tmp/）

上一版把脚本放在 `tmp/`，而 `tmp/` 在 `.gitignore` 里，评审拿到仓库后没法重录。本目录是入库版本，路径已改成从仓库根算起（`Path(__file__).resolve().parents[2]`）。

## 目录内容

| 文件 | 作用 |
|---|---|
| `story2.py` | 旁白台本（26 条，键名 + 文案）。改文案后必须删掉 `tmp/voice4/` 里同名键的 mp3，否则会命中旧音频 |
| `synth_edge.py` | 用 edge-tts（`zh-CN-YunxiNeural`，+10%）逐条合成旁白，产出 `tmp/voice4/*.mp3` 与 `meta.json`；单次成功率低，脚本内部重试到成功并校验「字/秒」区间 |
| `arch_layers.html` | 架构讲解页（五层 / 三条流 / 界面分层对照 / 代码层号 L5→L0 / 系统自检数字），录屏时以 `file://` 打开 |
| `record_final.py` | Playwright 一次连续录完整条演示：起 demo 库（`make_demo_db.py --target tmp/final.db --force`）、起 uvicorn（带 `API_TOKEN=rec`）、逐页断言无 401、录出 `tmp/finalvideo/*.webm` 与带时间轴的 `tmp/cues.json` |
| `mux_final.py` | webm → mp4，旁白按每条 cue 起点 `adelay` 铺到一条音轨，生成 `tmp/final.srt` |
| `mux_subs3.py` | 用 drawtext 烧单行字幕（长句按标点比例切段，画面永远一行），产出 `tmp/final_voice_subs.mp4` |
| `update_docs_video.py` | 把成片真实时长/字节/条数写回 `README.md`、`docs/提交物对照表.md`、`docs/交付/比赛演示方案.md` |

## 重录步骤（仓库根执行）

```bash
.venv/bin/python scripts/demo_video/synth_edge.py       # 1. 合成旁白 -> tmp/voice4/
.venv/bin/python scripts/demo_video/record_final.py     # 2. 一次连续录屏 -> tmp/finalvideo/*.webm + tmp/cues.json
.venv/bin/python scripts/demo_video/mux_final.py       # 3. 视频+旁白+字幕文件 -> tmp/final_silent.mp4 tmp/final_audio.m4a tmp/final.srt
.venv/bin/python scripts/demo_video/mux_subs3.py       # 4. 烧单行字幕 -> tmp/final_voice_subs.mp4
cp tmp/final_voice_subs.mp4 'docs/交付/演示录屏_全功能.mp4'
cp tmp/final.srt 'docs/交付/演示录屏_字幕.srt'
.venv/bin/python scripts/demo_video/update_docs_video.py  # 5. 回填文档数字
.venv/bin/python scripts/check_docs_numbers.py           # 6. 数字一致性自检
```

## 这版修掉的两个老毛病

1. **页签打不开（401）**：录屏时服务端必须用 `API_TOKEN=rec` 启动，页面用 `http://127.0.0.1:<port>/#token=rec` 打开；前端会把 hash 里的令牌写进 sessionStorage 再抹掉 hash。令牌不匹配时，账务/计划/治理三页每张卡都会显示「这块没加载出来」，对话页顶部会挂黄色告警条。`record_final.py` 现在起服务后逐页断言没有「没加载出来 / 接口鉴权没通过 / 加载失败」再继续录。
2. **字幕重复两行**：老流水线是分段录制后拼接，`caps_all.json` 跨轮追加导致同一句 cue 出现两份（只差十几毫秒），字幕因此在画面上叠成两行黑底。现在改成**一次连续录完整条**，cue 与视频时间轴同源，天然不会重复；`mux_final.py` 生成的 SRT 还会检查无重叠、无重复文案。

## 注意

- 录屏依赖 Playwright（`python3.12` 环境已装）与 `ffmpeg`/`ffprobe`。
- `record_final.py` 会自己起 uvicorn（端口写在脚本里）并用 `tmp/final.db`（由 `scripts/make_demo_db.py` 重新生成），**不会**碰 `data/bank.db`。
- 录屏画面与旁白之间有约 1.8 秒的 webm 预卷偏移，`record_final.py` 会把偏移写进 `tmp/cues.json`，合流时统一补偿，抽帧 QA 要按同一偏移取帧。
- 演示库的丰富数据由 `scripts/make_demo_db.py` 负责（多目标 / 多计划 / 多提醒 / 接管单 / 拒绝记录 / 异常核实 / 适当性 / 订阅裁决 / 储蓄扣款历史）。