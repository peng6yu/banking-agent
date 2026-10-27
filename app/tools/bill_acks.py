"""账单异常的核实记录：核实、撤销，以及「这张卡上已经核实过哪些笔」。

为什么单独一个文件：检测规则（`app/tools/bill.py`）是纯读的启发式，核实是用户看过
之后的决定，两者生命周期不同；检测那边也已经贴到 600 行上限。

按「单笔流水」存而不是按异常组存：同一组以后又添了新的一笔，那一笔用户没看过，
整组就不该算已核实——按笔存，这个判断自然成立。

只做免打扰：不写钱、不改流水、不认欺诈。核实结论分两档（自己刷的 / 不是我刷的），
因为「不是我刷的」后面要接冻结卡和找银行争议，混成一档就把这层意思丢了。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Final

from app import audit, timefmt
from app.clock import ValidationError, now_ms
from app.tools.queries import tx_view

#: 用户看过的两种结论。
VERDICTS: Final[tuple[str, ...]] = ("mine", "not_mine")
VERDICT_LABELS: Final[dict[str, str]] = {"mine": "我自己刷的", "not_mine": "不是我刷的"}
#: 「已核实」列表最多回多少条：这是收尾区，不是流水账。
MAX_ACKED_ITEMS: Final[int] = 20


def acked_pairs(conn: sqlite3.Connection, account_id: str) -> dict[str, set[str]]:
    """这张卡上已经核实过的流水：按规则分组，值是被核实过的流水号。"""
    rows = conn.execute(
        "SELECT kind, tx_id FROM anomaly_acks WHERE account_id = ?", (str(account_id),)
    ).fetchall()
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(str(row["kind"]), set()).add(str(row["tx_id"]))
    return out


def drop_acked(
    conn: sqlite3.Connection, account_id: str, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """整组证据都核实过的异常不再算待核实；有一笔没核实过就保留整组。"""
    acked = acked_pairs(conn, account_id)
    kept: list[dict[str, Any]] = []
    for item in items:
        known = acked.get(str(item.get("kind") or ""), set())
        ids = {str(tx_id) for tx_id in item.get("tx_ids") or []}
        if ids and ids <= known:
            continue
        kept.append(item)
    return kept


def _ack_account(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, user_id FROM accounts WHERE id = ?", (str(account_id),)).fetchone()
    if row is None:
        raise ValidationError(
            "找不到这个账户，可能已经被销户或者编号不对。",
            reason="ACCOUNT_NOT_FOUND",
            field="account_id",
        )
    return row


def _wanted_tx_ids(conn: sqlite3.Connection, account: sqlite3.Row, tx_ids: list[str]) -> list[str]:
    """取出要处理的流水号，并确认它们确实属于这张卡。"""
    wanted = [str(tx).strip() for tx in tx_ids or [] if str(tx).strip()]
    if not wanted:
        raise ValidationError("没有告诉我要处理哪几笔流水。", reason="ANOMALY_TX_EMPTY", field="tx_ids")
    marks = ",".join("?" for _ in wanted)
    known = {
        str(row["id"])
        for row in conn.execute(
            f"SELECT id FROM transactions WHERE account_id = ? AND id IN ({marks})",
            [str(account["id"]), *wanted],
        ).fetchall()
    }
    missing = [tx for tx in wanted if tx not in known]
    if missing:
        raise ValidationError(
            "这几笔流水不在这张卡上，刷新后重新点一次。",
            reason="ANOMALY_TX_NOT_FOUND",
            field="tx_ids",
        )
    return wanted


def ack_anomaly(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    kind: str,
    tx_ids: list[str],
    note: str = "",
    verdict: str = "mine",
) -> dict[str, Any]:
    """记下用户对这一组流水的结论：只免打扰，不动钱、不改流水。

    同一笔再点一次不算新增，但**结论可以改**（先点「我自己刷的」后来想起来不对，
    再点「不是我刷的」要能覆盖），所以这里用 upsert 而不是 INSERT OR IGNORE。
    """
    verdict = str(verdict or "mine").strip()
    if verdict not in VERDICTS:
        raise ValidationError(
            "核实结论只能是「我自己刷的」或「不是我刷的」。",
            reason="ANOMALY_VERDICT_UNKNOWN",
            field="verdict",
        )
    account = _ack_account(conn, account_id)
    wanted = _wanted_tx_ids(conn, account, tx_ids)
    marks = ",".join("?" for _ in wanted)
    seen = {
        str(row["tx_id"])
        for row in conn.execute(
            f"SELECT tx_id FROM anomaly_acks WHERE account_id = ? AND kind = ? AND tx_id IN ({marks})",
            [str(account["id"]), str(kind), *wanted],
        ).fetchall()
    }
    stamp = now_ms()
    for tx_id in wanted:
        conn.execute(
            "INSERT INTO anomaly_acks (user_id, account_id, kind, tx_id, note, verdict, created_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, account_id, kind, tx_id)"
            " DO UPDATE SET verdict = excluded.verdict, note = excluded.note,"
            " created_ms = excluded.created_ms",
            (
                str(account["user_id"]),
                str(account["id"]),
                str(kind),
                tx_id,
                str(note or "")[:200],
                verdict,
                stamp,
            ),
        )
    created = len([tx for tx in wanted if tx not in seen])
    audit.append(
        conn,
        "bill.anomaly_acked",
        {
            "kind": str(kind),
            "account_id": str(account["id"]),
            "tx_ids": wanted,
            "verdict": verdict,
            "created": created,
            "reacked": len(wanted) - created,
            "note": str(note or "")[:200],
        },
    )
    return {
        "kind": str(kind),
        "account_id": str(account["id"]),
        "tx_ids": wanted,
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "acked_count": len(wanted),
        "created": created,
        "reacked": len(wanted) - created,
    }


def unack_anomaly(
    conn: sqlite3.Connection, *, account_id: str, kind: str, tx_ids: list[str]
) -> dict[str, Any]:
    """撤销核实：这一组重新挂回待核实（首页、账单页、明细标红三处一起回来）。"""
    account = _ack_account(conn, account_id)
    wanted = _wanted_tx_ids(conn, account, tx_ids)
    marks = ",".join("?" for _ in wanted)
    cursor = conn.execute(
        f"DELETE FROM anomaly_acks WHERE account_id = ? AND kind = ? AND tx_id IN ({marks})",
        [str(account["id"]), str(kind), *wanted],
    )
    removed = max(0, int(cursor.rowcount or 0))
    audit.append(
        conn,
        "bill.anomaly_unacked",
        {
            "kind": str(kind),
            "account_id": str(account["id"]),
            "tx_ids": wanted,
            "removed": removed,
        },
    )
    return {
        "kind": str(kind),
        "account_id": str(account["id"]),
        "tx_ids": wanted,
        "removed": removed,
    }


def acked_rows(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    """「已核实」清单：最近核实的排在前面，带流水本身的事实，好让用户认得出是哪一笔。"""
    rows = conn.execute(
        "SELECT a.kind AS kind, a.verdict AS verdict, a.note AS note, a.created_ms AS acked_ms,"
        " t.id AS id, t.account_id AS account_id, t.direction AS direction,"
        " t.amount_fen AS amount_fen, t.counterparty AS counterparty, t.category AS category,"
        " t.note AS tx_note, t.channel AS channel, t.ts_ms AS ts_ms"
        " FROM anomaly_acks a JOIN transactions t ON t.id = a.tx_id"
        " WHERE a.account_id = ?"
        " ORDER BY a.created_ms DESC, t.ts_ms DESC LIMIT ?",
        (str(account_id), MAX_ACKED_ITEMS),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        view = tx_view(row)
        verdict = str(row["verdict"] or "mine")
        items.append(
            {
                "kind": str(row["kind"] or ""),
                "tx_id": str(view["transaction_id"]),
                "ts_ms": int(view["ts_ms"]),
                "ts_text": str(view["ts_text"]),
                "counterparty": str(view["counterparty"]),
                "category": str(view["category"]),
                "direction": str(view["direction"]),
                "amount_fen": int(view["amount_fen"]),
                "amount_yuan": str(view["amount_yuan"]),
                "verdict": verdict,
                "verdict_label": VERDICT_LABELS.get(verdict, "我自己刷的"),
                "note": str(row["note"] or ""),
                "acked_ms": int(row["acked_ms"]),
                "acked_text": timefmt.stamp_text(int(row["acked_ms"])),
            }
        )
    return items


__all__ = [
    "MAX_ACKED_ITEMS",
    "VERDICTS",
    "VERDICT_LABELS",
    "ack_anomaly",
    "acked_pairs",
    "acked_rows",
    "drop_acked",
    "unack_anomaly",
]
