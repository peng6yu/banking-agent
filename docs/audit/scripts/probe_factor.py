
from __future__ import annotations
import json, time
import bx

c = bx.client()
sid = "s_audit6"
st = {}

def on_ev(name, data):
    if name in ("confirm", "factor"):
        st.update(cid=data["confirmation_id"], required=data.get("required") or [], exp=data.get("expires_in_s"))
        print(f"   >>> [{name}] cid={st['cid']} required={st['required']} expires_in_s={st['exp']}")

print("余额(前):", bx.balance(c))
evs, t = bx.chat(c, "转 600 元给张三，尾号4417", sid, on_event=on_ev, verbose=False)
for _ in range(120):
    if st.get("cid"):
        break
    time.sleep(0.5)
cid = st.get("cid")
print("cid:", cid, "required:", st.get("required"))
if not cid:
    raise SystemExit("no cid")

def issue(factor):
    r = c.post("/api/factor/challenge", json={"session_id": sid, "confirmation_id": cid, "factor": factor})
    b = r.json()
    ch = (b.get("challenges") or [{}])[0]
    print(f"   issue({factor}) {r.status_code}: challenge={ch.get('challenge_id')} attempts_left={ch.get('attempts_left')} len_code={len(str(ch.get('code') or ''))}")
    return ch

def submit(proofs, approve=True):
    s, body = bx.confirm(c, sid, cid, approve, proofs)
    print("   confirm ->", s, json.dumps(body, ensure_ascii=False)[:260])
    return s, body

print("\n== 1) face 连错 5 次 ==")
a = issue("face")
for i in range(5):
    submit([{"factor": "face", "challenge_id": a["challenge_id"], "code": "000000"}])
print("   第6次：同一挑战的正确码")
submit([{"factor": "face", "challenge_id": a["challenge_id"], "code": a["code"]}])

print("\n== 2) 重新获取 face 验证码（看 attempts 是否重置 = 锁定可绕） ==")
b2 = issue("face")
submit([{"factor": "face", "challenge_id": b2["challenge_id"], "code": b2["code"]}])

print("\n== 3) face+sms 各一张新挑战，正确码执行 ==")
fa = issue("face"); sm = issue("sms")
submit([{"factor": "face", "challenge_id": fa["challenge_id"], "code": fa["code"]},
        {"factor": "sms", "challenge_id": sm["challenge_id"], "code": sm["code"]}])
print("余额(后):", bx.balance(c))
t.join(timeout=40)
print("终帧:", [n for n, _ in evs][-3:])
for n, d in evs:
    if n in ("done", "error"):
        print("  ", n, json.dumps(d, ensure_ascii=False)[:220])
