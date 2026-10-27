
from __future__ import annotations
import json
import bx
c = bx.client()
ov = c.get("/api/business/overview").json()
print("top keys:", list(ov.keys()))
print(json.dumps(ov.get("handoffs"), ensure_ascii=False)[:900])
print("\nplans:", json.dumps(ov.get("plans"), ensure_ascii=False)[:500])
print("\nsavings:", json.dumps(ov.get("savings"), ensure_ascii=False)[:300])
print("\nquick_actions:", json.dumps(ov.get("quick_actions"), ensure_ascii=False)[:400])
print("\nalerts:", json.dumps(ov.get("alerts"), ensure_ascii=False)[:400])
