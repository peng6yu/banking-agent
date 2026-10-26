
from __future__ import annotations
import json, time
import bx
c = bx.client()
def show(tag, r):
    try: body = r.json()
    except Exception: body = r.text[:200]
    print(f"{tag}: {r.status_code} {json.dumps(body, ensure_ascii=False)[:400]}")

show("plan 正常", c.post("/api/plans", json={"title":"体检用计划","steps":[{"kind":"check","title":"看一眼余额"}]}))
show("plan 13 步", c.post("/api/plans", json={"title":"体检13步","steps":[{"kind":"check","title":f"第{i}步"} for i in range(13)]}))
show("plan kind 乱写", c.post("/api/plans", json={"title":"体检乱kind","steps":[{"kind":"hack","title":"x"}]}))
show("plan max_attempts 11", c.post("/api/plans", json={"title":"体检attempts","steps":[{"kind":"check","title":"x","max_attempts":11}]}))

now = int(time.time()*1000)
d = c.get("/api/plans").json()
print("\nplans:", d.get("count"))
for p in d.get("plans", []):
    steps = p.get("steps") or []
    for s in steps:
        due = s.get("due_ms")
        flag = ""
        if due: flag = f"  due-now={round((due-now)/60000)}min"
        print(f"  {p.get('plan_id')} {p.get('title')[:24]!r} status={p.get('status')} kind={s.get('kind')} due_ms={due}{flag}")
