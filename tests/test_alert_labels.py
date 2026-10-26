"""提醒原因码：字典里有就用人话，没有也不许把机器码印到界面上。"""

from __future__ import annotations

import app.db as db
from app import proactive
from app.views.queue import _alerts


def _setup(conn) -> None:
    with db.tx(conn):
        conn.execute(
            "INSERT INTO users (id, name, phone, created_ms) VALUES ('u_1', '演示', '13800000001', 1)"
        )
        conn.execute(
            "INSERT INTO savings_goals (id, user_id, name, target_fen, target_date, saved_fen,"
            " status, created_ms, updated_ms)"
            " VALUES ('sg_1', 'u_1', '换相机', 600000, '2027-03-31', 0, 'active', 1, 1)"
        )
        for alert_id, reason in (
            ("pa_known", proactive.REASON_BUDGET_OVERSPENT),
            ("pa_odd", "SOMETHING_NEW"),
        ):
            conn.execute(
                "INSERT INTO proactive_alerts (id, goal_id, rule_id, reason, evidence_json,"
                " suggestion, created_ms, status, resolved_ms, user_id)"
                " VALUES (?, 'sg_1', NULL, ?, '{}', '', 1, 'open', NULL, 'u_1')",
                (alert_id, reason),
            )


def test_an_unknown_alert_reason_never_shows_the_machine_code(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "alerts.db"))
    try:
        db.init_db(conn)
        _setup(conn)
        view = _alerts(conn, "u_1")
        labels = {item["reason"]: item["reason_text"] for item in view["items"]}
        assert labels[proactive.REASON_BUDGET_OVERSPENT] == "本月花了超过预算"
        assert labels["SOMETHING_NEW"] == "有一条提醒需要你看一眼"
        # reason 字段本身是给前端分组用的机器码，可以留在响应里；
        # 但给人看的 reason_text 里不许出现英文机器码。
        assert all(
            not any("A" <= ch <= "z" for ch in item["reason_text"]) for item in view["items"]
        )
    finally:
        conn.close()
