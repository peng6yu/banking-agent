
from __future__ import annotations
import json, time
import bx
c = bx.client()
def show(tag, r):
    try: body = r.json()
    except Exception: body = r.text[:200]
    print(f"{tag}: {r.status_code} {json.dumps(body, ensure_ascii=False)[:300]}")

print("== 提醒（钱相关） ==")
show("每月25还信用卡", c.post("/api/reminders", json={"text":"每月 25 号还信用卡","repeat":"monthly","day":25,"hour":9}))
show("订阅扣费提醒", c.post("/api/reminders", json={"text":"爱奇艺扣费提醒","due_ms":int(time.time()*1000)+86400_000}))
show("还款日+过去时间", c.post("/api/reminders", json={"text":"还信用卡","due_ms":int(time.time()*1000)-86400_000}))

print("\n== 计划 happy ==")
show("plan 正常", c.post("/api/plans", json={"title":"体检用计划","steps":[{"text":"看一眼余额"}]}))
show("plan step 缺字段", c.post("/api/plans", json={"title":"体检用计划2","steps":[{"foo":"bar"}]}))
