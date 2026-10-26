
from __future__ import annotations
import json
import bx

c = bx.client()

def show(tag, r):
    try: body = r.json()
    except Exception: body = r.text[:200]
    print(f"{tag}: {r.status_code} {json.dumps(body, ensure_ascii=False)[:220]}")

print("== handoffs ==")
r = c.get("/api/handoffs"); show("GET /api/handoffs", r)
try:
    for h in r.json().get("handoffs", [])[:5]:
        print("   ", {k: h.get(k) for k in ("id","status","reason","run_id","created_at_text","source")})
except Exception as e: print("  parse fail", e)

print("\n== 手动记一笔边界 ==")
for amt in (0, -100, 100000001, 100000000):
    show(f"manual amount_fen={amt}", c.post("/api/transactions/manual", json={"amount_fen": amt, "direction":"out", "merchant":"测试", "category":"餐饮", "occurred_at_text":"2026-10-26 12:00"}))
show("manual 缺字段", c.post("/api/transactions/manual", json={}))
show("manual direction 乱写", c.post("/api/transactions/manual", json={"amount_fen":100,"direction":"sideways","merchant":"测试","category":"餐饮","occurred_at_text":"2026-10-26 12:00"}))

print("\n== 提醒 ==")
show("reminder 空标题", c.post("/api/reminders", json={"title":"","at_text":"2026-12-01 09:00"}))
show("reminder 标题61字", c.post("/api/reminders", json={"title":"长"*61,"at_text":"2026-12-01 09:00"}))
show("reminder 过去时间", c.post("/api/reminders", json={"title":"过去的事","at_text":"2020-01-01 09:00"}))
show("reminder 非法时间", c.post("/api/reminders", json={"title":"乱时间","at_text":"明天早上"}))

print("\n== 计划 ==")
show("plan 0 步", c.post("/api/plans", json={"title":"空计划","steps":[]}))
show("plan 13 步", c.post("/api/plans", json={"title":"太多步","steps":[{"kind":"check","text":f"第{i}步"} for i in range(13)]}))
show("plan 标题61字", c.post("/api/plans", json={"title":"长"*61,"steps":[{"kind":"check","text":"看余额"}]}))
show("plan 正常", c.post("/api/plans", json={"title":"体检用计划","steps":[{"kind":"check","text":"看一眼余额"}]}))

print("\n== 储蓄目标 ==")
show("goal 0", c.post("/api/savings/goals", json={"name":"零目标","target_fen":0,"target_date":"2027-01-01"}))
show("goal 负", c.post("/api/savings/goals", json={"name":"负目标","target_fen":-100,"target_date":"2027-01-01"}))
show("goal 名41字", c.post("/api/savings/goals", json={"name":"名"*41,"target_fen":1000,"target_date":"2027-01-01"}))
show("goal 日期乱", c.post("/api/savings/goals", json={"name":"日期乱","target_fen":1000,"target_date":"明年"}))
show("goal 正常", c.post("/api/savings/goals", json={"name":"体检目标","target_fen":100000,"target_date":"2027-06-30"}))

print("\n== 适当性问卷 ==")
show("suitability 空", c.post("/api/investment/suitability", json={}))
show("suitability 乱答案", c.post("/api/investment/suitability", json={"answers":[{"question_id":"q1","option_key":"zzz"}]}))

print("\n== 订阅定性 ==")
show("verdict 缺 kind", c.post("/api/subscriptions/verdict", json={"merchant":"爱奇艺会员"}))
