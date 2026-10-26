
from __future__ import annotations
import json
import bx
c = bx.client()
def show(tag, r):
    try: body = r.json()
    except Exception: body = r.text[:200]
    print(f"{tag}: {r.status_code} {json.dumps(body, ensure_ascii=False)[:260]}")

print("== 生日计划 ==")
show("budget 0", c.post("/api/plans/birthday", json={"birthday":"2026-12-01","budget_fen":0}))
show("budget 负", c.post("/api/plans/birthday", json={"birthday":"2026-12-01","budget_fen":-500}))
show("生日格式乱", c.post("/api/plans/birthday", json={"birthday":"2026/12/01","budget_fen":100000}))
show("生日已过", c.post("/api/plans/birthday", json={"birthday":"2020-01-01","budget_fen":100000}))
show("生日 2 月 30", c.post("/api/plans/birthday", json={"birthday":"2027-02-30","budget_fen":100000}))
show("项目金额 > 预算", c.post("/api/plans/birthday", json={"birthday":"2026-12-01","budget_fen":20000,"items":[{"key":"flower","label":"鲜花","merchant":"花店","amount_fen":50000}]}))
show("项目 5 个", c.post("/api/plans/birthday", json={"birthday":"2026-12-01","budget_fen":100000,"items":[{"key":f"k{i}","label":"物品","merchant":"店","amount_fen":100} for i in range(5)]}))
show("项目金额 0", c.post("/api/plans/birthday", json={"birthday":"2026-12-01","budget_fen":100000,"items":[{"key":"flower","label":"鲜花","merchant":"花店","amount_fen":0}]}))
show("标题 61 字", c.post("/api/plans/birthday", json={"birthday":"2026-12-01","title":"长"*61}))
show("happy(无项目)", c.post("/api/plans/birthday", json={"birthday":"2026-12-01","budget_fen":100000}))
show("happy(有项目+授权)", c.post("/api/plans/birthday", json={"birthday":"2026-12-25","budget_fen":100000,"items":[{"key":"flower","label":"鲜花","merchant":"花店","amount_fen":40000},{"key":"cake","label":"蛋糕","merchant":"烘焙店","amount_fen":60000}],"save_authorization":True}))
print("\n== 计划列表 ==")
r=c.get("/api/plans"); 
try:
    d=r.json(); print("plans count:", len(d.get("plans") or []), "keys:", list(d.keys()))
    for p in (d.get("plans") or [])[:6]: print("  ", p.get("plan_id"), p.get("title") or p.get("name"), p.get("status"), p.get("kind"))
except Exception as e: print("parse", e, r.text[:200])
print("\n== 提醒 happy ==")
show("reminder 正常", c.post("/api/reminders", json={"title":"体检提醒","at_text":"2026-12-01 09:00"}))
show("reminder 空 body", c.post("/api/reminders", json={}))
