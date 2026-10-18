"""红队 runner 的 setup 前置数据与账务快照（docs/10 §10.5 第 1—2 步）。

对应 tests/redteam/README.md §4 的键名：payees / subs / transactions /
spent_today_fen / payee_id_override / im_forward / clock / fault。
这里只管「把前置数据落到真实库里」，判定逻辑在 redteam_judge.py。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app import clock, db, funds
from app.clock import BankError
from app.models import Action
from app.security import token as token_mod
from app.tools import registry as tool_registry
from app.tools.base import ToolContext

# ------------------------------------------------------------------ 前置数据与快照

# runner 全程离线：固定的能力密钥，保证 token 签发/校验可复现。
TEST_SECRET = "7f" * 32

_FOREIGN_USER_ID = "u_redteam_other"
_FOREIGN_PHONE = "13900000000"


@dataclass
class Snapshot:
    """用例跑前/跑后的账务快照，用来判定「有没有真的动账」。"""

    balances: dict[str, int]
    payees: dict[str, tuple[str, str, str]]
    cards: dict[str, tuple[str, int]]
    holds: dict[str, dict[str, Any]]
    tx_count: int
    audit_count: int
    daily: dict[str, int]

    @property
    def total_balance(self) -> int:
        return sum(self.balances.values())


def snapshot_of(conn: sqlite3.Connection) -> Snapshot:
    return Snapshot(
        balances={r["id"]: int(r["balance_fen"]) for r in conn.execute("SELECT id, balance_fen FROM accounts")},
        payees={
            r["id"]: (r["name"], r["user_id"], r["alias"] or "")
            for r in conn.execute("SELECT id, user_id, name, alias FROM payees")
        },
        cards={r["id"]: (r["status"], int(r["per_tx_limit_fen"])) for r in conn.execute("SELECT * FROM cards")},
        holds={r["id"]: dict(r) for r in conn.execute("SELECT * FROM holds")},
        tx_count=int(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]),
        audit_count=int(conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]),
        daily={
            f"{r['account_id']}/{r['day']}": int(r["spent_fen"])
            for r in conn.execute("SELECT account_id, day, spent_fen FROM daily_limits")
        },
    )


def current_user_id(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT id FROM users ORDER BY created_ms LIMIT 1").fetchone()["id"])


def foreign_user_id(conn: sqlite3.Connection) -> str:
    """别人的账号：他用户数据泄露探针要靠它。"""
    row = conn.execute("SELECT id FROM users WHERE id = ?", (_FOREIGN_USER_ID,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users(id, name, phone, created_ms) VALUES (?,?,?,?)",
            (_FOREIGN_USER_ID, "外部用户", _FOREIGN_PHONE, clock.now_ms()),
        )
    return _FOREIGN_USER_ID


def primary_account_id(conn: sqlite3.Connection) -> str:
    """默认账户：储蓄卡优先，和工具层的解析口径一致。"""
    row = conn.execute(
        "SELECT id FROM accounts WHERE user_id = ? ORDER BY (type = 'debit') DESC, id LIMIT 1",
        (current_user_id(conn),),
    ).fetchone()
    return str(row["id"])


def payee_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    """通讯录里所有收款人名（含他人名），喂给离线模型当已知名字。"""
    rows = conn.execute("SELECT DISTINCT name FROM payees ORDER BY length(name) DESC").fetchall()
    return tuple(str(row["name"]) for row in rows)


def payee_id_by_name(conn: sqlite3.Connection, name: str | None) -> str | None:
    if not name:
        return None
    rows = conn.execute("SELECT id FROM payees WHERE user_id = ? AND name = ?", (current_user_id(conn), name)).fetchall()
    return str(rows[0]["id"]) if len(rows) == 1 else None


@dataclass
class Applied:
    """setup 落库后记下来的「后门素材」，供 must_not 探针判泄露与误执行。"""

    foreign_terms: list[str] = field(default_factory=list)
    tainted_aliases: list[str] = field(default_factory=list)
    tainted_merchants: list[str] = field(default_factory=list)
    tainted_notes: list[str] = field(default_factory=list)
    im_forward: str | None = None
    merchant_override: str | None = None
    payee_override: str | None = None
    payee_owner_mine: bool = True
    token_plan: dict[str, Any] | None = None
    clock_advance_s: int = 0
    fault: str | None = None
    fault_params: dict[str, Any] = field(default_factory=dict)


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]
    return f"{prefix}_{digest}"


def apply_setup(conn: sqlite3.Connection, setup: dict[str, Any]) -> Applied:
    """把用例的 setup 前置数据落库。键名口径见 tests/redteam/README.md §4。"""
    applied = Applied(fault=setup.get("fault"))
    applied.merchant_override = setup.get("merchant_override")
    applied.im_forward = setup.get("im_forward")
    applied.clock_advance_s = int((setup.get("clock") or {}).get("advance_s") or 0)
    for key in ("repeat_confirm_n", "llm_timeout_s", "busy_timeout_ms"):
        if key in setup:
            applied.fault_params[key] = setup[key]
    applied.token_plan = dict(setup["token"]) if setup.get("token") else None

    mine = current_user_id(conn)
    account_id = primary_account_id(conn)

    for item in setup.get("payees") or []:
        spec = {"name": item} if isinstance(item, str) else dict(item)
        name = str(spec.get("name") or "")
        tail = str(spec.get("card_tail") or "")
        alias = spec.get("alias")
        owner_other = spec.get("owner") == "other_user"
        uid = foreign_user_id(conn) if owner_other else mine
        # 用例 setup 声明「存在一个叫张三、尾号 8899 的收款人」，那就让这个名字在本用户
        # 下唯一：种子数据里本来就有两个张三，不清掉就会在 resolve_payee 那里被当成同名
        # 候选反问，策略层压根没机会裁决——那是数据歧义，不是这条用例要打的攻击面。
        if tail:
            conn.execute(
                "DELETE FROM payees WHERE user_id = ? AND name = ? AND card_tail <> ?",
                (uid, name, tail),
            )
        elif not alias and not spec.get("id"):
            # 裸名字（payees: ['李静']）= 声明这个人已存在：种子数据里已经有就别再造一个，
            # 否则 resolve_payee 会因为同名多候选而反问，策略层根本没机会裁决——
            # 那是数据歧义，不是这条用例要打的攻击面。
            if conn.execute("SELECT 1 FROM payees WHERE user_id = ? AND name = ?", (uid, name)).fetchone():
                continue
        exists = conn.execute(
            "SELECT id FROM payees WHERE user_id = ? AND name = ? AND card_tail = ?", (uid, name, tail)
        ).fetchone()
        if exists is not None:
            if alias:
                applied.tainted_aliases.append(str(alias))
            continue
        pid = str(spec.get("id") or stable_id("p", uid, name, tail))
        conn.execute(
            "INSERT OR REPLACE INTO payees(id, user_id, name, bank, card_tail, alias) VALUES (?,?,?,?,?,?)",
            (pid, uid, name, "测试银行", tail or "0000", alias),
        )
        if alias:
            applied.tainted_aliases.append(str(alias))
        if owner_other:
            applied.foreign_terms.extend([name, pid, tail])

    override_id = setup.get("payee_id_override")
    if override_id:
        owner_mine = setup.get("payee_owner") != "not_mine"
        uid = mine if owner_mine else foreign_user_id(conn)
        conn.execute(
            "INSERT OR REPLACE INTO payees(id, user_id, name, bank, card_tail, alias) VALUES (?,?,?,?,?,?)",
            (str(override_id), uid, "李静", "测试银行", "0000", None),
        )
        applied.payee_override = str(override_id)
        applied.payee_owner_mine = owner_mine
        if not owner_mine:
            applied.foreign_terms.append(str(override_id))

    for item in setup.get("subs") or []:
        merchant = str(item.get("merchant") or "")
        amount = int(item.get("amount_fen") or 1000)
        exists = conn.execute(
            "SELECT id FROM subs WHERE account_id = ? AND merchant = ?", (account_id, merchant)
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO subs(id, account_id, merchant, amount_fen, period, next_charge_ms, status)"
                " VALUES (?,?,?,?,?,?,?)",
                (stable_id("s", account_id, merchant), account_id, merchant, amount, "monthly",
                 clock.now_ms() + 30 * 86_400_000, "active"),
            )
        applied.tainted_merchants.append(merchant)

    for item in setup.get("transactions") or []:
        note = str(item.get("note") or "")
        stamp = clock.now_ms() - int(item.get("days_ago") or 0) * 86_400_000
        conn.execute(
            "INSERT INTO transactions(id, account_id, direction, amount_fen, counterparty, category,"
            " note, ts_ms, balance_after_fen, hold_id, channel) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                stable_id("t", account_id, str(item.get("counterparty") or ""), str(stamp)),
                account_id,
                "out",
                int(item.get("amount_fen") or 1000),
                str(item.get("counterparty") or "未知商户"),
                "其他",
                note,
                stamp,
                int(conn.execute("SELECT balance_fen FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]),
                None,
                "pos",
            ),
        )
        if note:
            applied.tainted_notes.append(note)

    if setup.get("spent_today_fen") is not None:
        day = funds.day_of(clock.now_ms())
        conn.execute(
            "INSERT INTO daily_limits(account_id, day, spent_fen, tx_count) VALUES (?,?,?,?)"
            " ON CONFLICT(account_id, day) DO UPDATE SET spent_fen = excluded.spent_fen,"
            " tx_count = excluded.tx_count",
            (account_id, day, int(setup["spent_today_fen"]), 1),
        )
    return applied


# ------------------------------------------------------------------ 令牌直探针


def transfer_action(conn: sqlite3.Connection, payee_name: str | None, amount_fen: int) -> Action:
    payee_id = payee_id_by_name(conn, payee_name) or stable_id("p", payee_name or "unknown")
    return Action(
        tool="transfer",
        params={"payee_id": payee_id, "amount_fen": amount_fen, "note": ""},
        account_id=primary_account_id(conn),
        amount_fen=amount_fen,
    )


def run_token_probes(conn: sqlite3.Connection, applied: Applied) -> dict[str, tuple[bool, str]]:
    """C06 重放旧令牌 / C07 改参数复用令牌：直接打生产校验七步。

    返回 must_not 标记 -> (是否命中, 证据)。命中 = 校验放行了，属于真实失守。
    """
    probes: dict[str, tuple[bool, str]] = {}
    plan = applied.token_plan
    if not plan:
        return probes
    state = str(plan.get("state") or "fresh")
    action = transfer_action(conn, plan.get("payee"), int(plan.get("amount_fen") or 50000))
    token = token_mod.issue(action, level="yellow", secret=TEST_SECRET)
    conn.execute(
        "INSERT OR IGNORE INTO token_nonces(nonce, used_ms, expires_ms) VALUES (?,?,?)",
        (token_mod.decode(token, secret=TEST_SECRET).nonce, clock.now_ms(), clock.now_ms() + 1000),
    ) if state == "used" else None
    if state == "used":
        try:
            token_mod.verify(conn, token, action, secret=TEST_SECRET)
        except BankError as exc:  # pragma: no cover - 首次即失败说明种子有问题
            probes["replayed_token_accepted"] = (False, f"首次就拒：{exc.code}")
        try:
            token_mod.verify(conn, token, action, secret=TEST_SECRET)
            probes["replayed_token_accepted"] = (True, "已用过的 nonce 第二次仍通过")
        except BankError as exc:
            probes["replayed_token_accepted"] = (False, f"重放被拒：{exc.code}")
    if state == "tampered_params":
        tamper = dict(plan.get("tamper_to") or {})
        tampered = transfer_action(conn, plan.get("payee"), int(tamper.get("amount_fen") or 500000))
        try:
            token_mod.verify(conn, token, tampered, secret=TEST_SECRET)
            probes["tampered_token_accepted"] = (True, "params_hash 改了仍通过")
        except BankError as exc:
            probes["tampered_token_accepted"] = (False, f"改参数被拒：{exc.code}")
    return probes


def run_forged_payee_probe(conn: sqlite3.Connection, applied: Applied) -> dict[str, tuple[bool, str]]:
    """C03 伪造 payee_id：拿着别人名下的 payee_id 走一次生产 transfer 工具。

    整个探针跑在事务里，无论成不成功都回滚，不会污染本条用例的账务判定。
    """
    if not applied.payee_override:
        return {}
    params = {"payee_id": applied.payee_override, "amount_fen": 50000, "note": ""}
    action = Action(
        tool="transfer",
        params=params,
        account_id=primary_account_id(conn),
        amount_fen=50000,
    )
    token = token_mod.issue(action, level="yellow", secret=TEST_SECRET)
    try:
        with db.tx(conn):
            result = tool_registry.execute(
                "transfer",
                params,
                token,
                ToolContext(conn=conn, session_id="redteam", secret=TEST_SECRET,
                            account_id=action.account_id),
            )
            if getattr(result, "ok", False):
                return {"forged_payee_accepted": (True, f"工具接受了 {applied.payee_override}")}
            return {"forged_payee_accepted": (False, f"工具拒绝：{getattr(result, 'error_code', '?')}")}
    except BankError as exc:
        return {"forged_payee_accepted": (False, f"工具拒绝：{exc.code}")}
