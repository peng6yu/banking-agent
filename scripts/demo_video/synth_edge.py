# -*- coding: utf-8 -*-
"""用 edge-tts 的原音色（zh-CN-YunxiNeural +10%）重配旁白。

edge 的合成接口今天频繁被重置，单次成功率约 1/6，所以每条都重试到成功，
并用「字/秒在 3.8–6.5 之间」判定不是半截音频。
"""
import asyncio, glob, json, os, subprocess, sys
from pathlib import Path
import edge_tts

ROOT = str(Path(__file__).resolve().parents[2])
OUT = os.path.join(ROOT, "tmp/voice4")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from story2 import STORY

VOICE = "zh-CN-YunxiNeural"
RATE = "+10%"


def dur(path):
    o = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True).stdout
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
    os.makedirs(OUT, exist_ok=True)
    meta = {}
    for i, (key, text) in enumerate(STORY):
        dst = os.path.join(OUT, "%02d-%s.mp3" % (i, key))
        hit = None
        for cand in sorted(glob.glob(os.path.join(OUT, "*-%s.mp3" % key))):
            if dur(cand) > 1.5:
                hit = cand
                break
        if hit:
            d = dur(hit)
            meta[key] = {"text": text, "dur": d, "file": os.path.basename(hit), "chars": len(text)}
            print("%-12s cached %.2fs (%s)" % (key, d, os.path.basename(hit)), flush=True)
            continue
        d, attempts = await synth(text, dst)
        meta[key] = {"text": text, "dur": d, "file": os.path.basename(dst), "chars": len(text)}
        print("%-12s %3d chars %.2fs %.2f cps (attempts %d)" % (key, len(text), d, len(text) / d, attempts), flush=True)
    total = sum(v["dur"] for v in meta.values())
    json.dump({"total": round(total, 2), "items": meta}, open(os.path.join(OUT, "meta.json"), "w"),
              ensure_ascii=False, indent=1)
    print("TOTAL %.2fs" % total)


asyncio.run(main())