# -*- coding: utf-8 -*-
"""v8 旁白合成：只补新改的 10 条，其余 33 条沿用 v7 已合成的音频。

为什么要"沿用"：edge-tts 今天的合成接口频繁被重置，单次成功率约 1/6，
43 条全量重合成要耗很久，而且同样文案重合成出来时长会飘。
所以这里按"文案逐字相同"判定可复用——文案改了一个字，就重新合成那一条。

用法：.venv/bin/python scripts/demo_video/synth_edge_v8.py
产出：tmp/voice6/*.mp3 + tmp/voice6/meta.json（total / items[key]={text,dur,file,chars}）
"""
import asyncio, glob, json, os, shutil, subprocess, sys
from pathlib import Path
import edge_tts

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tmp/voice6"
SEED = ROOT / "tmp/voice4"          # v7 的成品音频，文案没变的直接从这里搬
sys.path.insert(0, str(Path(__file__).resolve().parent))
from story_v8 import STORY  # noqa: E402

VOICE = "zh-CN-YunxiNeural"
RATE = "+10%"


def dur(path):
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
    try:
        return round(float(o.strip()), 2)
    except ValueError:
        return 0.0


async def synth(text, dst):
    for attempt in range(1, 61):
        try:
            await edge_tts.Communicate(text, VOICE, rate=RATE).save(dst)
            d = dur(dst)
            cps = len(text) / d if d else 0
            if d > 1.5 and 3.8 <= cps <= 7.0:
                return d, attempt
            print("    bad take %.2fs cps=%.1f, retry" % (d, cps), flush=True)
        except Exception as e:
            if attempt % 5 == 0:
                print("    %s (attempt %d)" % (type(e).__name__, attempt), flush=True)
        await asyncio.sleep(1.0)
    raise SystemExit("gave up: " + text[:20])


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    seed_meta = {}
    seed_meta_path = SEED / "meta.json"
    if seed_meta_path.exists():
        seed_meta = json.loads(seed_meta_path.read_text())["items"]

    meta = {}
    reused = synthesized = 0
    for i, (key, text) in enumerate(STORY):
        dst = OUT / ("%02d-%s.mp3" % (i, key))
        hit = None
        for cand in sorted(glob.glob(str(OUT / ("*-%s.mp3" % key)))):
            if dur(cand) > 1.5:
                hit = cand
                break
        if hit:
            meta[key] = {"text": text, "dur": dur(hit), "file": os.path.basename(hit), "chars": len(text)}
            print("%-12s cached  %.2fs (%s)" % (key, meta[key]["dur"], meta[key]["file"]), flush=True)
            reused += 1
            continue

        # 文案逐字相同 -> 搬 v7 的成品，不重新合成
        prev = seed_meta.get(key)
        if prev and prev.get("text") == text:
            src = SEED / prev["file"]
            if src.exists():
                shutil.copyfile(src, dst)
                meta[key] = {"text": text, "dur": dur(dst), "file": dst.name, "chars": len(text)}
                print("%-12s reused  %.2fs (from voice4)" % (key, meta[key]["dur"]), flush=True)
                reused += 1
                continue

        d, attempts = await synth(text, dst)
        meta[key] = {"text": text, "dur": d, "file": dst.name, "chars": len(text)}
        print("%-12s tts     %.2fs (attempt %d)" % (key, d, attempts), flush=True)
        synthesized += 1

    total = round(sum(m["dur"] for m in meta.values()), 2)
    (OUT / "meta.json").write_text(json.dumps({"total": total, "items": meta}, ensure_ascii=False, indent=1))
    print("voice6 meta 写入：%d 条，总长 %.2fs（复用 %d / 新合成 %d）" % (len(meta), total, reused, synthesized))


if __name__ == "__main__":
    asyncio.run(main())
