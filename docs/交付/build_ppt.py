#!/usr/bin/env python3.12
"""生成 docs/交付/答辩PPT.pptx（12 页 / ≤10 分钟 / 5 张产品实拍）。

用法：/home/peng/.local/bin/python3.12 docs/交付/build_ppt.py

纪律：
- 页面上每个数字要么标出处，要么写显式占位，不编造；
- 截图全部来自 docs/交付/截图/信息架构/ 的本机实测产物；
- 页数/时长/结构变化时，同步 docs/答辩PPT大纲.md 与 docs/交付/交付说明.md。
"""
from pathlib import Path

from lxml import etree
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

BASE = Path(__file__).resolve().parent
IMG = BASE / "截图" / "四入口_2026-10-27"   # 四入口版（2026-10-27 重拍）
IMG_LEGACY = BASE / "截图" / "信息架构"       # 六视图旧版，仅作历史对照
OUT = BASE / "答辩PPT.pptx"
TOTAL = 13

NAVY = RGBColor(0x0F, 0x29, 0x42)
INK = RGBColor(0x1F, 0x29, 0x37)
MUTED = RGBColor(0x6B, 0x72, 0x80)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT = RGBColor(0xC7, 0xD6, 0xE4)
GREEN = RGBColor(0x16, 0xA3, 0x4A)
YELLOW = RGBColor(0xD9, 0x77, 0x06)
RED = RGBColor(0xDC, 0x26, 0x26)
TINT = RGBColor(0xF3, 0xF6, 0xF9)
C = {"i": INK, "m": MUTED, "g": GREEN, "y": YELLOW, "r": RED, "w": WHITE, "n": NAVY}
FONT = "微软雅黑"


def style(run, size, bold=False, color=INK):
    font = run.font
    font.size = Pt(size)
    font.bold = bold
    font.color.rgb = color
    font.name = FONT
    rpr = run._r.get_or_add_rPr()
    if rpr.find(qn("a:ea")) is None:
        etree.SubElement(rpr, qn("a:ea"), typeface=FONT)


def para(tf, text, size=16, bold=False, color=INK, before=5, align=None):
    first = tf.paragraphs[0]
    node = first if (len(tf.paragraphs) == 1 and not first.runs) else tf.add_paragraph()
    run = node.add_run()
    run.text = text
    style(run, size, bold, color)
    node.space_before = Pt(before)
    node.space_after = Pt(0)
    if align is not None:
        node.alignment = align
    return node


def box(slide, left, top, width, height):
    frame = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height)).text_frame
    frame.word_wrap = True
    return frame


def rect(slide, left, top, width, height, fill, shape=MSO_SHAPE.RECTANGLE):
    obj = slide.shapes.add_shape(shape, Inches(left), Inches(top), Inches(width), Inches(height))
    obj.fill.solid()
    obj.fill.fore_color.rgb = fill
    obj.line.fill.background()
    obj.shadow.inherit = False
    return obj


def blank(prs):
    return prs.slides.add_slide(prs.slide_layouts[6])


def header(slide, title, index):
    rect(slide, 0, 0, 13.333, 0.96, NAVY)
    para(box(slide, 0.6, 0.16, 11.4, 0.64), title, 24, True, WHITE, 0)
    para(box(slide, 11.45, 0.3, 1.4, 0.4), "%02d / %02d" % (index, TOTAL), 12, False, LIGHT, 0, PP_ALIGN.RIGHT)


def footer(slide, spec, index):
    para(box(slide, 0.6, 6.9, 12.13, 0.42), spec.get("src", ""), 8.5, False, MUTED, 0)
    rect(slide, 0.6, 6.86, 12.13, 0.015, RGBColor(0xD1, 0xD5, 0xDB))
    para(box(slide, 0.6, 7.08, 6.4, 0.3), "第 %d / %d 页 · 累计 %s / 10:00" % (index, TOTAL, spec["cum"]), 10, False, MUTED, 0)
    para(box(slide, 6.6, 7.08, 6.13, 0.3), "banking-agent · 2026 金融科技大赛人工智能赛道", 10, False, MUTED, 0, PP_ALIGN.RIGHT)


def fit_picture(slide, name, left, top, max_w, max_h):
    """按比例把截图放进一个矩形区域，返回 (x, y, w, h)。"""
    path = IMG / name
    if not path.exists():
        path = IMG_LEGACY / name
    with Image.open(path) as im:
        iw, ih = im.size
    scale = min(max_w / iw, max_h / ih)
    width, height = iw * scale, ih * scale
    x = left + (max_w - width) / 2
    slide.shapes.add_picture(str(path), Inches(x), Inches(top), Inches(width), Inches(height))
    return x, top, width, height


def caption(slide, text, x, y, width):
    if text:
        para(box(slide, x, y, width, 0.32), text, 9.5, False, MUTED, 0)


def draw_cover(slide, spec):
    rect(slide, 0, 0, 13.333, 3.05, NAVY)
    para(box(slide, 0.8, 0.7, 11.9, 1.0), spec["t"], 30, True, WHITE, 0)
    para(box(slide, 0.8, 1.62, 11.7, 0.7), spec["sub"], 19, False, LIGHT, 0)
    frame = box(slide, 0.85, 3.25, 7.1, 3.0)
    for tag, key in (("三色权限闸门", "g"), ("一次性能力令牌", "y"), ("只追加审计哈希链", "r")):
        para(frame, "▪ " + tag, 18, True, C[key], 10)
    para(frame, spec["sub2"], 12.5, False, MUTED, 16)
    para(frame, spec["note"], 12.5, False, MUTED, 5)
    x, y, w, h = fit_picture(slide, "main_02_confirm_card.png", 8.1, 3.25, 4.6, 3.2)
    caption(slide, "决策剧场与确认卡（本机实拍）", 8.1, y + h + 0.06, 4.6)


def draw_arch(slide, spec):
    rect(slide, 0.6, 1.3, 12.13, 3.75, TINT)
    for left, head, body, tint in spec["boxes"]:
        card = rect(slide, left, 1.65, 3.45, 2.75, WHITE, MSO_SHAPE.ROUNDED_RECTANGLE)
        card.line.color.rgb = tint
        card.line.width = Pt(2)
        para(box(slide, left + 0.18, 1.76, 3.1, 0.55), head, 15, True, tint, 0)
        frame = box(slide, left + 0.18, 2.34, 3.1, 1.9)
        for line in body:
            para(frame, line, 12, False, INK, 6)
    for left in (4.3, 8.5):
        rect(slide, left, 2.9, 0.5, 0.5, NAVY, MSO_SHAPE.RIGHT_ARROW)
    rect(slide, 0.85, 4.62, 11.6, 0.55, NAVY, MSO_SHAPE.ROUNDED_RECTANGLE)
    para(box(slide, 1.0, 4.7, 11.3, 0.42), spec["band"], 14, True, WHITE, 0)


def draw_items(slide, spec, index):
    frame = box(slide, 0.6, 1.22, 12.1, 4.9 if spec.get("chips") else 5.2)
    for level, text, key in spec["items"]:
        size = 15 if level == 0 else 12.5
        para(frame, ("▪ " if level == 0 else "– ") + text, size, key in "gr", C[key], 9 if level == 0 else 4)
    if spec.get("chips"):
        draw_chips(slide, spec["chips"], 4.42)
    if spec.get("talk"):
        rect(slide, 0.6, 5.78, 0.06, 0.7, GREEN)
        para(box(slide, 0.85, 5.78, 11.9, 0.78), spec["talk"], 13.5, False, INK, 0)


def draw_chips(slide, chips, top):
    """指标条：四个大数字 + 小标签，给纯文字页一点视觉锚点。"""
    width, gap = 2.83, 0.17
    for i, (big, label) in enumerate(chips[:4]):
        left = 0.6 + i * (width + gap)
        card = rect(slide, left, top, width, 0.95, TINT, MSO_SHAPE.ROUNDED_RECTANGLE)
        card.line.color.rgb = RGBColor(0xD8, 0xDF, 0xE7)
        card.line.width = Pt(0.75)
        para(box(slide, left + 0.12, top + 0.08, width - 0.24, 0.5), big, 22, True, NAVY, 0)
        para(box(slide, left + 0.12, top + 0.56, width - 0.24, 0.32), label, 10, False, MUTED, 0)


def draw_split(slide, spec, index):
    frame = box(slide, 0.6, 1.22, 5.55, 5.0)
    for level, text, key in spec["items"]:
        size = 12.8 if level == 0 else 11.2
        para(frame, ("▪ " if level == 0 else "– ") + text, size, key in "gr", C[key], 8 if level == 0 else 3)
    if spec.get("talk"):
        rect(slide, 0.6, 5.9, 0.06, 0.6, GREEN)
        para(box(slide, 0.85, 5.9, 5.0, 0.7), spec["talk"], 12, False, INK, 0)
    x, y, w, h = fit_picture(slide, spec["img"], 6.35, 1.22, 6.55, 4.95)
    caption(slide, spec.get("cap", ""), 6.35, y + h + 0.08, 6.55)


def draw_cols(slide, spec, index):
    widths = (3.55, 3.55, 3.55)
    lefts = (0.6, 4.75, 8.9)
    for (head, color, lines), left, width in zip(spec["cols"], lefts, widths):
        card = rect(slide, left, 1.3, width, 3.75, WHITE, MSO_SHAPE.ROUNDED_RECTANGLE)
        card.line.color.rgb = color
        card.line.width = Pt(2)
        para(box(slide, left + 0.15, 1.45, width - 0.3, 0.55), head, 15, True, color, 0)
        frame = box(slide, left + 0.15, 2.02, width - 0.3, 2.95)
        for line in lines:
            para(frame, "· " + line, 10.8, False, INK, 4)
    if spec.get("foot"):
        rect(slide, 0.6, 5.22, 0.06, 0.42, NAVY)
        para(box(slide, 0.85, 5.22, 11.9, 0.5), spec["foot"], 11.5, False, MUTED, 0)
    if spec.get("talk"):
        rect(slide, 0.6, 5.85, 0.06, 0.55, GREEN)
        para(box(slide, 0.85, 5.85, 11.9, 0.65), spec["talk"], 12.5, False, INK, 0)


SLIDES = [
    {
        "layout": "cover",
        "t": "银行 AI 智能体 · 敢让 Agent 动钱的银行系统",
        "sub": "2026 金融科技大赛 · 人工智能赛道 · 国内赛道",
        "sub2": "仓库 github.com/peng6yu/banking-agent ｜ 一条命令可跑 ｜ 模拟账本 · 全部数据为本地种子虚构",
        "note": "口播：今天不演示「AI 能不能转账」，演示「AI 凭什么可以转账，以及凭什么不能」。",
        "cum": "0:15",
        "src": "安全声明见 README.md；本页无运行数据。",
    },
    {
        "layout": "items",
        "t": "痛点与定位：银行敢让它在模拟账本里动钱",
        "items": [
            (0, "银行 App 的 AI 客服只能查：用户每一步都要手动点，业务跑不完", "i"),
            (0, "能转账的「智能体」靠 prompt 约束：prompt 是建议不是边界，被绕过一次就出事", "i"),
            (0, "出事之后查不清：审计散在应用日志里，可改、可删、不可追责", "i"),
            (0, "定位：别人做更会聊天的助手，我们做银行敢让它在模拟账本里动钱的 Agent。", "r"),
            (1, "三件事同时成立才叫可用：跑得完（40%）／拦得住（30%）／看得见（30%）", "m"),
            (1, "六个赛题场景全部有真实后端：转账 / 账单 / 理财 / 卡片 / 订阅 / 跨场景", "m"),
            (0, "效率：传统手机银行 3+ 步 → 本系统 1 句 + 1 次确认", "g"),
            (1, "脚本桩到确认卡 7–8ms；真模型意图解析约 1.5–3s/次（历史实测，非本次复跑）", "m"),
        ],
        "chips": [("6 / 6", "赛题场景有真实后端"), ("35", "受管工具"), ("1857", "自动化测试通过"), ("43 / 43", "红队用例通过")],
        "talk": "只做到第一件就是玩具；三件一起成立，银行才敢在模拟账本里把动钱权限交给它。",
        "cum": "0:55",
        "src": "评审权重 40/30/30 见赛题 §4；场景覆盖与边界见 docs/提交物对照表.md §三。",
    },
    {
        "layout": "split",
        "t": "六个场景：六类能力都有真实后端",
        "items": [
            (0, "① 转账：人名/手机号/尾号消歧、AA 拆分、定时、冲正", "i"),
            (0, "② 账单：月/年分类、Top 商户、三类异常", "i"),
            (0, "③ 理财：5 题适当性 → C1–C5、拒售、申购/赎回红灯", "i"),
            (0, "④ 卡片：虚拟卡、限额、冻结/解冻、挂失、改密、销卡", "i"),
            (0, "⑤ 订阅：流水推断 + 7 天内扣费 + 唯一匹配取消", "i"),
            (0, "⑥ 跨场景：生日计划 → 预算预留 → 到点提醒/授权", "i"),
            (1, "全部数字来自后端只读接口；取不到显示「暂不可用」，不垫示例数据。", "m"),
            (1, "边界：3 理财无横向对比/持仓收益；5 订阅无推送；6 到点只提醒 —— 不把 6/6 说成产品级", "r"),
        ],
        "img": "overview_desktop.png",
        "cap": "财务总览：余额 / 收支 / 异常 / 计划，全部来自后端接口（本机实拍）",
        "cum": "1:45",
        "src": "场景实现与测试见 docs/提交物对照表.md §三；截图 scripts/ui_screenshots.py 产出。",
    },
    {
        "layout": "arch",
        "t": "三段论：模型永远碰不到钱",
        "boxes": [
            (0.75, "不可信区 LLM 意图/规划", ["只产出「申请」：", "意图 + 槽位 + DAG", "不产出「执行」"], RED),
            (4.95, "裁决区 PolicyEngine", ["确定性纯函数：", "无网络、无模型", "三色判定 + 签发令牌"], YELLOW),
            (9.15, "执行区 工具/资金", ["只认令牌，无令牌不动", "hold → commit 两阶段", "审计哈希链只增不改"], GREEN),
        ],
        "band": "决策剧场（SSE 实时流式）　＋　审计哈希链（只增不改、可追责）",
        "cum": "2:30",
        "src": "出处：app/main.py / app/policy/rules.py / app/security/token.py；架构见 docs/技术文档.md §2。",
    },
    {
        "layout": "split",
        "t": "决策剧场：一句话，看到它凭什么被批准",
        "items": [
            (0, "左栏说话，右栏五块同屏：意图 / DAG / 三色 / 因子 / 审计", "i"),
            (0, "SSE 流式：start / intent / trace / confirm / factor / tool / done / error", "m"),
            (0, "零依赖零构建前端：壳 + 样式 + 21 个模块脚本；断网也能演", "g"),
            (1, "五块默认常驻不折叠；确认卡三档动作（确认/改一下再发/拒绝）+ 授权绑定摘要 + 超时视为拒绝", "g"),
            (1, "静态合规检查 95/95（21 个脚本）；7 条浏览器旅程逐条断言 DOM + 服务端状态", "m"),
            (0, "同名收款人：两个「张三」必须停下来问，模型无权替用户选", "y"),
        ],
        "img": "1_助手.png",
        "cap": "助手默认页：一句话之后右栏五块同屏；空态给绿/黄/红三句示例（2026-10-27 本机实拍）",
        "talk": "评委不用翻日志，就看它凭什么。",
        "cum": "3:30",
        "src": "出处：web/index.html；95/95 为 scripts/check_frontend.py 实测；旅程见 docs/交付/browser_journeys.json。",
    },
    {
        "layout": "split",
        "t": "三色闸门与日累计：边界写死在代码里",
        "items": [
            (0, "🟢 R01 只读：余额/流水/账单/订阅/卡 → 自动执行", "g"),
            (0, "🟡 R06：日累计 ≤1000 元 → 一次确认，令牌 120 秒", "y"),
            (0, "🔴 R07–R10：越线/大额/挂失/申购 → 人脸+短信", "r"),
            (1, "日累计按「用户」而非账户：999.99 黄 / 1000.00 黄 / 1000.01 红", "m"),
            (1, "越线不是拒绝线：红色强验证通过后真实出账；绝对上限 R13 默认关闭", "m"),
        ],
        "img": "red_factor_fresh.png",
        "cap": "红灯实拍：挂失银行卡 → R09 判定「需要多因子」，人脸+短信+动态口令，依据与理由都写在面板上",
        "talk": "颜色不是模型给的，是裁决区的纯函数给的。",
        "cum": "4:20",
        "src": "出处：app/policy/rules.py（R01–R13，R13 默认 None）；tests/test_daily_risk_tiers.py（14 条）。",
    },
    {
        "layout": "items",
        "t": "能力令牌：把模型换成恶意的也动不了钱",
        "items": [
            (0, "七步校验：字段齐备 → HMAC 签名 → 未过期 → nonce 未用过 → 工具一致 → 参数指纹 → 账户/金额", "r"),
            (1, "120 秒、一次性；换收款人、换工具、改金额都等于换一笔", "m"),
            (0, "密钥改错：确认卡照常弹出，点确认立刻 MISSING_CAPABILITY —— 一分钱不动，拒绝进审计", "y"),
            (0, "MOCK_MODE=true 固定恶意计划（转出全部余额）：意图判 unknown → 反问，零工具调用", "y"),
            (0, "模型可换，闸门不可换：换模型只影响「说得好不好」，不影响「能不能动钱」。", "g"),
        ],
        "chips": [("7 步", "令牌逐项校验"), ("120 秒", "一次性有效"), ("0 元", "无令牌可动"), ("100%", "拒绝都留审计")],
        "talk": "prompt 被绕过一次，系统最多变难用；令牌被绕过一次，连一分钱都动不了。",
        "cum": "5:10",
        "src": "出处：app/security/token.py 七步校验；MOCK_MODE 见 README「离线与对抗演示」；tests/test_token.py。",
    },
    {
        "layout": "split",
        "t": "审计哈希链：出了事查得清，而且可验证",
        "items": [
            (0, "每条：seq / ts / session / event / payload / prev_hash / hash", "i"),
            (0, "hash = sha256(prev_hash + ts + event + payload)；触发器禁 UPDATE/DELETE", "g"),
            (0, "一条转账 11 个事件全链可回放；/api/audit/verify 返回 ok=true", "i"),
            (1, "66 类事件闭集：写不进白名单的名字会被拒绝", "m"),
        ],
        "img": "security_desktop.png",
        "cap": "执行记录与安全：审计链可逐条展开（本机实拍）",
        "talk": "安全不是「挡住了多少」，是「失败时往哪边倒、事后能不能查」。",
        "cum": "5:50",
        "src": "出处：app/audit.py（哈希链与只增不改触发器）；tests/test_audit.py；66 类见 EVENT_TYPES。",
    },
    {
        "layout": "split",
        "t": "跨场景：生日计划不是一次性 DAG",
        "items": [
            (0, "建计划 → 预算预留（占可支配金额，不是假冻结）", "i"),
            (0, "生日前 2 天 09:00 触发检查 + 提醒；未授权时付款重新确认", "i"),
            (0, "一次性授权绑定计划版本 + 项目 + 金额；改动即失效", "y"),
            (0, "默认只提醒；显式一次性授权且重判绿/黄才自动付款，红/变更暂停", "r"),
            (1, "刷新/重启后计划仍在；J4 旅程覆盖建计划 + 刷新恢复", "m"),
        ],
        "img": "journey_04_birthday_reloaded.png",
        "cap": "生日计划刷新后仍在（J4 旅程实拍）",
        "talk": "跨场景不是把三个功能串起来，是让一句话跨过多个闸门而规则不变形。",
        "cum": "6:40",
        "src": "出处：app/birthday.py / app/plans.py / plan_authorizations；tests/test_birthday_budget.py、test_birthday_authorization.py。",
    },
    {
        "layout": "cols",
        "t": "把「智能」落在可验证的地方",
        "cols": [
            ("订阅推断", YELLOW, [
                "6 个月流水 → 同商户、间隔≈月/年、金额稳定",
                "给置信度 + 证据笔，不是黑盒判断",
                "近 7 天可能扣费；名字唯一匹配才允许取消",
                "只能停银行侧代扣，界面写明不能注销商户会员",
                "证据：tests/test_subscriptions.py",
            ]),
            ("投资者适当性", RED, [
                "5 题问卷 → C1–C5；未测评/超等级一律拒买",
                "产品列表/风险等级/申购赎回；R10 红灯三因子",
                "问卷是演示简化版，不是监管口径",
                "拒售也是「正确完成」，不是失败",
                "证据：tests/test_suitability.py（17 条）",
            ]),
            ("人工接管", GREEN, [
                "7 态队列；条件 UPDATE，同一任务只有一个操作员",
                "恢复前重查卡片/授权/账户/余额",
                "不重放已完成的资金节点；失败落 failed",
                "operator 只是审计字符串，无 RBAC（如实说）",
                "证据：tests/test_handoff.py",
            ]),
        ],
        "foot": "对应测试全绿：tests/test_subscriptions.py（15 条）/ test_suitability.py（17 条）/ test_handoff.py（13 条）；旅程 J3/J4/J5 覆盖同名消歧、生日计划、提醒处理。",
        "talk": "这三件事都不是「再加一个功能」，是把边界做成可复核的证据。",
        "cum": "7:20",
        "src": "出处：app/subscriptions.py、app/suitability.py、app/handoffs.py；对应测试 test_subscriptions.py / test_suitability.py / test_handoff.py。",
    },
    {
        "layout": "items",
        "t": "红队 43 条与证据链：不靠一张嘴",
        "items": [
            (0, "43 条五类：诱导转账 10 / 越权 10 / 注入 10 / 信息套取 8 / 异常与稳定性 5", "i"),
            (0, "PASS 43 / FAIL 0（2026-10-25 复跑；离线 ScriptedAdapter，约 12 秒，自动判定七列表）", "g"),
            (1, "历史 14 条 FAIL 已逐条归因：0 产品缺陷 / 8 夹具口径 / 6 设计取舍；must_not 全过、资金零变动", "m"),
            (0, "1857 passed / 4 skipped ｜ e2e 13/13 ｜ 浏览器旅程 7/7 ｜ 前端 95/95", "i"),
            (0, "异常熔断：5 分钟窗口 3 次安全拒绝 → 锁定 60 秒，半开只放行一次试探", "i"),
            (0, "审计链篡改当场被数据库触发器拒绝；verify 仍返回 ok=true", "i"),
        ],
        "chips": [("43 / 43", "红队用例 PASS"), ("1719", "pytest passed"), ("7 / 7", "浏览器旅程"), ("13 / 13", "e2e 步数")],
        "talk": "安全不是「我们挡住了多少攻击」，是「攻击面在哪一层、失败时系统往哪边倒」。",
        "cum": "8:10",
        "src": "出处：tests/redteam/cases.yaml（43 条）、scripts/redteam*；pytest 2026-10-26 终测；旅程 docs/交付/browser_journeys.json。",
    },
    {
        "layout": "split",
        "t": "出事之后：链能验、单能接",
        "items": [
            (0, "审计链逐行重算：改一条历史记录 → 校验立刻报「没通过」，并给出条目数与链头", "r"),
            (0, "实测：拆掉 append-only 触发器改 payload_json，再点「校验」→ 条目 43、链头 e814c683，界面直接说「从断点往前都不能再当证据」", "i"),
            (0, "人工接管：工单队列按状态给动作（我来处理 / 拒绝 / 继续跑 / 需要补充 / 取消），并发只有一个 claim 成功", "g"),
            (1, "四类拒绝都留痕：越权、注入、熔断、令牌不符，各有中文标签，进入拒绝列表与审计", "m"),
            (1, "校验结果写回审计面板本身，不只落在对话里 —— 在哪一页点都看得见", "m"),
        ],
        "img": "audit_tamper_detected.png",
        "cap": "篡改实测：改一条历史记录后点「校验」，面板红字报链断（2026-10-27 本机实拍）",
        "talk": "安全做不到「事后可验」，前面所有拦截都只是自说自话。",
        "cum": "8:20",
        "src": "出处：app/audit.py:185 verify_chain；app/db.py:638 append-only 触发器；队列截图见 截图/四入口_2026-10-27/handoff_queue_actions.png。",
    },
    {
        "layout": "items",
        "t": "指标、不足与收尾：先把丑话说完",
        "items": [
            (0, "指标：35 工具 ｜ 13 规则 ｜ 37 表 ｜ 73 审计事件 ｜ 59 路由 ｜ 6 场景 ｜ 1857 测试 ｜ 43 红队 ｜ 13 e2e ｜ 7 旅程", "g"),
            (1, "模型：DeepSeek 主脑 + 离线脚本桩；provider_check 4/4；A/B 8 轮 80 例单次采样，方向翻转不宣称提升", "m"),
            (0, "不足（照实说）：真实短信/人脸未接（演示桩）；只有 Web，无独立 App/IM", "r"),
            (1, "规划是模板 DAG，不是开放域；单用户、无 RBAC；不可宣称已对齐生产银行", "r"),
            (1, "能力令牌密钥需配置，忘了配黄红灯 fail closed；后台节拍单进程，进程退出即停", "m"),
            (1, "知识层：40 术语 / 25 实体 / 22 关系 / 11 意图 / 10 工作流 / 34 来源；只参与理解与解释，不进裁决", "m"),
            (1, "沙箱=消除执行面（0 命中）不是容器；理财期限/流动性/费率为 contest_simulation；真实 MFA 为演示桩", "m"),
            (1, "全部数据为本地种子虚构，不接真实银行/支付", "m"),
            (0, "收尾：它知道自己还不能做什么，也知道自己为什么不能 —— 这两件事比多几个功能重要。", "r"),
        ],
        "talk": "欢迎现场追问：七步校验、43 条红队、审计链，我们都能当场跑。",
        "cum": "9:20",
        "src": "出处：README「已知限制」与「数字台账」；安全自评报告 R-01～R-20；manifest 见 docs/benchmark/release_manifest.json。",
    },
]


def build():
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    for index, spec in enumerate(SLIDES, start=1):
        slide = blank(prs)
        layout = spec["layout"]
        if layout == "cover":
            draw_cover(slide, spec)
        else:
            header(slide, spec["t"], index)
            if layout == "arch":
                draw_arch(slide, spec)
            elif layout == "split":
                draw_split(slide, spec, index)
            elif layout == "cols":
                draw_cols(slide, spec, index)
            else:
                draw_items(slide, spec, index)
        footer(slide, spec, index)
    prs.save(str(OUT))
    return OUT


if __name__ == "__main__":
    print("generated:", build())
