# -*- coding: utf-8 -*-
"""成片质检：时长/分辨率/码率、字幕是否重叠或重复、画面里有无红色报错条/黄色警告条、字幕是否叠成两行。

用法：.venv/bin/python scripts/demo_video/qa_video.py [mp4] [srt]
产出：tmp/qa/<key>.png 抽帧、tmp/qa/sheet_*.jpg 联系表、控制台报告
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[2]
MP4 = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs/交付/演示录屏_全功能.mp4"
SRT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "docs/交付/演示录屏_字幕.srt"
OUT = ROOT / "tmp/qa"
OUT.mkdir(parents=True, exist_ok=True)

def probe(p: Path) -> dict:
    raw = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
        capture_output=True, text=True).stdout
    return json.loads(raw)

def parse_srt(text: str) -> list[dict]:
    segs = []
    for block in text.strip().split("\n\n"):
        lines = [l for l in block.split("\n") if l.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        a, b = lines[1].split("-->")
        def sec(t: str) -> float:
            t = t.strip().replace(",", ".")
            h, m, s = t.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        segs.append({"n": int(lines[0]), "start": sec(a), "end": sec(b),
                     "text": "\n".join(lines[2:]).strip()})
    return segs

def band(mask: Image.Image, axis_rows: bool = True) -> list[tuple[int, int]]:
    """返回 mask 中连续 True 的行区间（行号区间）。"""
    w, h = mask.size
    px = mask.load()
    rows = []
    for y in range(h):
        hit = 0
        for x in range(0, w, 3):
            if px[x, y]:
                hit += 1
        rows.append(hit * 3)
    bands, start, prev = [], None, None
    for y, v in enumerate(rows):
        on = v > w * 0.30
        if on and start is None:
            start = y
        if not on and start is not None:
            bands.append((start, y - 1)); start = None
    if start is not None:
        bands.append((start, h - 1))
    # 字幕白字会把一条黑底横带从中间切开，间隔小于 20 行的算同一条
    merged = []
    for bnd in bands:
        if merged and bnd[0] - merged[-1][1] <= 30:
            merged[-1] = (merged[-1][0], bnd[1])
        else:
            merged.append(bnd)
    return merged

def analyse(img: Image.Image) -> dict:
    rgb = img.convert("RGB")
    r, g, b = rgb.split()
    # 报错条 .notice.bad -> background var(--red-soft) #fef3f2 = (254,243,242)
    # 三个通道要同时成立才算命中（早先用 composite 会把 r 那一路覆盖掉，页底冷灰也会被误判成红条）
    def mask(chan, lo, hi):
        return Image.eval(chan, lambda v: 255 if lo <= v <= hi else 0)

    m_red = ImageChops.darker(Image.eval(r, lambda v: 255 if v >= 248 else 0),
                              ImageChops.darker(mask(g, 222, 250), mask(b, 222, 250)))
    # 警告条 .notice.warn -> background var(--amber-soft) #fffaeb = (255,250,235)
    m_amber = ImageChops.darker(Image.eval(r, lambda v: 255 if v >= 250 else 0),
                                ImageChops.darker(mask(g, 244, 255), mask(b, 212, 242)))
    n_red = sum(m_red.histogram()[255:])
    n_amber = sum(m_amber.histogram()[255:])
    # 字幕条：画面下方 y>=760 的深色横带（drawtext box=1 黑底）
    sub = rgb.crop((0, 760, rgb.size[0], rgb.size[1]))
    sr, sg, sb = sub.split()
    dark = Image.eval(sr, lambda v: 255 if v < 70 else 0)
    dark = Image.composite(Image.eval(sg, lambda v: 255 if v < 70 else 0), Image.new("L", sub.size, 0), dark)
    dark = Image.composite(Image.eval(sb, lambda v: 255 if v < 70 else 0), Image.new("L", sub.size, 0), dark)
    bands = band(dark)
    return {"red_px": n_red, "amber_px": n_amber, "dark_bands": bands}

def main() -> None:
    info = probe(MP4)
    dur = float(info["format"]["duration"])
    size = MP4.stat().st_size
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    as_ = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    print(f"== 文件：{MP4.name}")
    print(f"   时长 {dur:.2f}s ({int(dur)//60}:{int(dur)%60:02d})  大小 {size:,} B")
    print(f"   视频 {vs['codec_name']} {vs['width']}x{vs['height']} {vs.get('avg_frame_rate')} "
          f"bitrate={int(vs.get('bit_rate', 0))//1000}kbps")
    if as_:
        print(f"   音频 {as_['codec_name']} {as_.get('sample_rate')}Hz "
              f"bitrate={int(as_.get('bit_rate', 0))//1000}kbps")
    print(f"   时长硬约束 ≤300s：{'✅ 通过' if dur <= 300 else '❌ 超时'}")

    segs = parse_srt(SRT.read_text(encoding='utf-8'))
    print(f"\n== 字幕：{len(segs)} 段")
    dup_overlap = []
    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        if b["start"] < a["end"] - 0.02:
            dup_overlap.append((a["n"], b["n"], round(a["end"] - b["start"], 3)))
    texts = {}
    for s in segs:
        texts.setdefault(s["text"], []).append(s["n"])
    dupt = {t: v for t, v in texts.items() if len(v) > 1}
    print(f"   时间重叠：{dup_overlap if dup_overlap else '无 ✅'}")
    print(f"   重复文案：{dupt if dupt else '无 ✅'}")
    multi = [s["n"] for s in segs if "\n" in s["text"]]
    print(f"   含换行（两行字幕）：{multi if multi else '无 ✅'}")

    data = json.loads((ROOT / "tmp/cues.json").read_text(encoding="utf-8"))
    cues = data["cues"]
    shift = data["shift"]
    scale = float(data.get("scale", 1.0))  # 成片按 scale 统一变速过，抽帧时间要一起除
    print(f"\n== 抽帧（每条 cue 中点，共 {len(cues)} 张）")
    frames = []
    for c in cues:
        t = min(dur - 0.2, max(0.0, (c["start"] + shift + (c["end"] - c["start"]) / 2) / scale))
        fp = OUT / f"{c['i']:02d}_{c['key']}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(MP4),
                        "-frames:v", "1", str(fp)], check=True)
        frames.append((c, fp))
    bad = []
    warm = []
    for c, fp in frames:
        img = Image.open(fp)
        a = analyse(img)
        flag = ""
        # 字幕区：两条以上黑底横带 = 字幕叠成了两行（这���可以机器判）
        if len(a["dark_bands"]) > 1:
            flag += f" 深色横带×{len(a['dark_bands'])}"
        if flag:
            bad.append((c["key"], flag.strip(), a["red_px"], a["amber_px"], a["dark_bands"]))
        # 报错条/警告条的颜色和卡片底色很像，像素数只能当线索，最终要人看联系表
        if a["red_px"] > 4000 or a["amber_px"] > 4000:
            warm.append((c["key"], a["red_px"], a["amber_px"]))
    for k, f, rp, ap, db in bad:
        print(f"   ❌ {k}: {f}  red={rp} amber={ap} bands={db}")
    if not bad:
        print("   ✅ 全部帧：字幕区只有一条黑底横带（没有叠成两行）")
    if warm:
        print("   · 暖色像素较多（可能是卡片底色，也可能是报错条，请看联系表确认）：")
        print("     " + "、".join("%s(red=%d,amber=%d)" % w for w in warm))
    print("   ⚠ 报错条/警告条最终要人看联系表：" + "、".join(str(p.relative_to(ROOT)) for p in sorted(OUT.glob("sheet_*.jpg"))))

    # 联系表：每张 12 帧
    TW, TH = 360, 225
    for s in range(0, len(frames), 12):
        chunk = frames[s:s + 12]
        cols, rows = 4, 3
        sheet = Image.new("RGB", (cols * TW, rows * TH), (20, 20, 24))
        for k, (c, fp) in enumerate(chunk):
            im = Image.open(fp).convert("RGB").resize((TW, TH))
            sheet.paste(im, ((k % cols) * TW, (k // cols) * TH))
        sp = OUT / f"sheet_{s//12:02d}.jpg"
        sheet.save(sp, quality=88)
        print("   联系表 ->", sp.relative_to(ROOT))

if __name__ == "__main__":
    main()
