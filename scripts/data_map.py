
"""数据地图生成器：把每一层的数据到底长什么样变成可复现的事实。

产出：tmp/data_map.json（机器可读）+ docs/audit/数据字典.md（人读）。
只读：不写 data/bank.db；采样用临时副本与独立 API_TOKEN。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENUM_COLS = ("status", "kind", "type", "state", "level", "method", "period",
             "direction", "category", "severity", "verdict", "source", "action", "role")
UNIT_SUFFIX = {
    "_fen": "分（整数，元=值/100）",
    "_ms": "毫秒（epoch，按服务器本地时区解释，展示走 timefmt）",
    "_json": "JSON 文本",
    "_text": "已格式化中文文本（服务端产出，前端直出）",
    "_yuan": "元字符串（千分位+两位小数，如 1,234.50）",
    "_pct": "百分比整数",
    "_count": "条数（本页口径，非全库）",
}
FENCE = chr(96) * 3


def all_files():
    return list((ROOT / "app").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py"))


SQL_WRITE = {
    "insert": re.compile(r"INSERT\s+INTO\s+([a-z_]+)", re.I),
    "update": re.compile(r"UPDATE\s+([a-z_]+)\s+SET", re.I),
    "delete": re.compile(r"DELETE\s+FROM\s+([a-z_]+)", re.I),
}
SQL_READ = re.compile(r"\bFROM\s+([a-z_]+)", re.I)


def sql_usage(table):
    writers, readers = [], []
    wanted = {table.lower()}
    for f in all_files():
        rel = f.relative_to(ROOT).as_posix()
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for pat in SQL_WRITE.values():
                for m in pat.finditer(line):
                    if m.group(1).lower() in wanted:
                        writers.append(rel + ":" + str(i))
            for m in SQL_READ.finditer(line):
                if m.group(1).lower() in wanted:
                    readers.append(rel + ":" + str(i))

    def uniq(items):
        seen, out = set(), []
        for x in items:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return uniq(writers)[:6], uniq(readers)[:6]


def db_section(conn):
    tables = []
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for (name,) in rows:
        cols = []
        for c in conn.execute("PRAGMA table_info(" + name + ")"):
            cid, cname, ctype, notnull, dflt, pk = c
            cols.append({"name": cname, "type": ctype or "", "notnull": bool(notnull),
                         "default": dflt, "pk": bool(pk)})
        indexes = []
        for idx in conn.execute("PRAGMA index_list(" + name + ")"):
            iname, unique = idx[1], bool(idx[2])
            icols = [r[2] for r in conn.execute("PRAGMA index_info(" + iname + ")")]
            indexes.append({"name": iname, "unique": unique, "cols": icols})
        fks = []
        for fk in conn.execute("PRAGMA foreign_key_list(" + name + ")"):
            fks.append({"col": fk[3], "ref_table": fk[2], "ref_col": fk[4]})
        triggers = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (name,))]
        try:
            count = conn.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
        except sqlite3.Error:
            count = None
        enums = {}
        for c in cols:
            stem = c["name"].split("_")[-1]
            if stem in ENUM_COLS and "json" not in c["name"] and c["type"].upper().startswith("TEXT"):
                try:
                    vals = [r[0] for r in conn.execute(
                        "SELECT DISTINCT " + c["name"] + " FROM " + name +
                        " WHERE " + c["name"] + " IS NOT NULL LIMIT 12")]
                except sqlite3.Error:
                    continue
                if vals:
                    enums[c["name"]] = vals
        writers, readers = sql_usage(name)
        tables.append({"name": name, "row_count": count, "columns": cols, "indexes": indexes,
                       "fks": fks, "triggers": triggers, "enums": enums,
                       "writers": writers, "readers": readers})
    return tables


def unit_of(col):
    for suf, label in UNIT_SUFFIX.items():
        if col["name"].endswith(suf):
            return label
    return ""


def flatten_schema(schema, prefix="", depth=0, out=None):
    if out is None:
        out = []
    if depth > 4 or not isinstance(schema, dict):
        return out
    if "$ref" in schema:
        return out
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            for sub in schema[key]:
                flatten_schema(sub, prefix, depth, out)
            return out
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    for name, spec in props.items():
        typ = spec.get("type") or ("ref" if "$ref" in spec else "")
        if typ == "array":
            item = spec.get("items") or {}
            typ = "array<" + (item.get("type") or "object") + ">"
        row = {"path": prefix + name, "type": typ, "required": name in required,
               "minimum": spec.get("minimum"), "maximum": spec.get("maximum"),
               "maxLength": spec.get("maxLength"), "enum": spec.get("enum"),
               "default": spec.get("default")}
        if typ == "object":
            flatten_schema(spec, prefix + name + ".", depth + 1, out)
            continue
        out.append(row)
    return out


def api_section(spec):
    ops = []
    schemas = (spec.get("components") or {}).get("schemas") or {}
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            params = []
            for p in op.get("parameters") or []:
                sch = p.get("schema") or {}
                params.append({"name": p.get("name"), "in": p.get("in"),
                               "required": bool(p.get("required")), "type": sch.get("type") or "",
                               "minimum": sch.get("minimum"), "maximum": sch.get("maximum"),
                               "enum": sch.get("enum"), "default": sch.get("default")})
            body = []
            rb = op.get("requestBody")
            if rb:
                for mt, spec2 in (rb.get("content") or {}).items():
                    sch = spec2.get("schema") or {}
                    if "$ref" in sch:
                        ref = sch["$ref"].split("/")[-1]
                        sch = schemas.get(ref) or {}
                    body.extend(flatten_schema(sch))
                    break
            ops.append({"method": method.upper(), "path": path,
                        "name": op.get("summary") or op.get("operationId") or "",
                        "tag": (op.get("tags") or [""])[0], "params": params, "body": body,
                        "writes": method.lower() in ("post", "patch", "put", "delete")})
    return ops


def infer_shape(value, depth=0, max_depth=4):
    if isinstance(value, bool):
        return "bool", value
    if isinstance(value, (int, float)):
        return "number", value
    if isinstance(value, str):
        return "string", (value[:40] + "…" if len(value) > 40 else value)
    if value is None:
        return "null", None
    if isinstance(value, list):
        if not value:
            return "array<empty>", None
        t, s = infer_shape(value[0], depth + 1, max_depth)
        return "array<" + str(t) + ">", s
    if isinstance(value, dict):
        if depth >= max_depth:
            return "object", "{…}"
        return "object", {k: infer_shape(v, depth + 1, max_depth) for k, v in value.items()}
    return "unknown", None


PATH_ID_SOURCES = {
    "sid": ("sessions", "id"), "session_id": ("sessions", "id"),
    "plan_id": ("plans", "id"), "handoff_id": ("handoffs", "id"),
    "message_id": ("messages", "id"), "scheduled_id": ("scheduled_transfers", "id"),
    "goal_id": ("savings_goals", "id"), "alert_id": ("proactive_alerts", "id"),
    "sub_id": ("subs", "id"), "confirmation_id": ("confirmations", "id"),
}
# 只采样可安全重放的写端点（全部在临时副本库上执行）
SAFE_WRITES = {
    "POST /api/messages/refresh",
    "POST /api/proactive/monitor/run",
    "POST /api/scheduled/run",
}


def _query_for(op, conn):
    q = {}
    for p in op["params"]:
        if p["in"] != "query":
            continue
        name = p["name"]
        if name in PATH_ID_SOURCES:
            table, col = PATH_ID_SOURCES[name]
            row = conn.execute("SELECT " + col + " FROM " + table + " LIMIT 1").fetchone()
            if row:
                q[name] = str(row[0])
        elif p.get("required") and p.get("enum"):
            q[name] = p["enum"][0]
    return q


def _resolve_path(path, conn):
    filled = path
    for name, (table, col) in PATH_ID_SOURCES.items():
        token = "{" + name + "}"
        if token in filled:
            row = conn.execute("SELECT " + col + " FROM " + table + " LIMIT 1").fetchone()
            if not row:
                return None
            filled = filled.replace(token, str(row[0]))
    return filled if "{" not in filled else None


def sample_responses(ops, db_path):
    import asyncio

    import httpx

    os.environ["API_TOKEN"] = "data-map-token"
    os.environ["SCHEDULED_TICK_MS"] = "0"
    os.environ["FACTOR_DEMO_ECHO"] = "1"
    from app import db as app_db
    from app.main import app

    tmpdir = Path(tempfile.mkdtemp(prefix="datamap-"))
    tmpdb = tmpdir / "bank.db"
    shutil.copyfile(db_path, tmpdb)
    app_db.DEFAULT_DB_PATH = tmpdb
    conn = sqlite3.connect(tmpdb)

    out = {}
    headers = {"X-API-Token": "data-map-token"}

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            # 写端点放最后，避免 refresh/monitor 影响只读采样
            ordered = sorted(ops, key=lambda o: (o["writes"], o["path"]))
            for op in ordered:
                key = op["method"] + " " + op["path"]
                if op["writes"] and key not in SAFE_WRITES:
                    continue
                target = _resolve_path(op["path"], conn)
                if target is None:
                    out[key] = {"status": -2, "note": "库里没有可用于路径参数的样本行"}
                    continue
                try:
                    if op["writes"]:
                        r = await client.post(target, headers=headers, json={})
                    else:
                        r = await client.get(target, headers=headers,
                                             params=_query_for(op, conn))
                except Exception as exc:
                    out[key] = {"status": -1, "error": type(exc).__name__}
                    continue
                try:
                    payload = r.json()
                except Exception:
                    payload = None
                out[key] = {"status": r.status_code,
                            "shape": infer_shape(payload) if payload is not None else {"type": "non-json"}}

    asyncio.run(run())
    conn.close()
    return out


def render_md(payload, spec):
    tables, ops, samples = payload["tables"], payload["ops"], payload["samples"]
    total_cols = sum(len(t["columns"]) for t in tables)
    lines = [
        "# 数据字典：每一层的数据到底长什么样",
        "",
        "> 自动生成（scripts/data_map.py）。生成命令：.venv/bin/python scripts/data_map.py",
        "> 不写 data/bank.db；响应采样走临时副本库与临时令牌。",
        "",
        "规模：**" + str(len(tables)) + " 张表 / " + str(total_cols) + " 个列 / " +
        str(len(ops)) + " 个接口**（" + str(len(spec["paths"])) + " 条路径）。",
        "",
        "## 0. 单位与命名约定",
        "",
        "| 后缀 | 含义 | 谁负责格式化 |",
        "|---|---|---|",
    ]
    who = {"_fen": "app/money.py:68-73 转 _yuan", "_ms": "app/timefmt.py",
           "_json": "json.dumps/loads（ensure_ascii=False）", "_text": "服务端一次成型",
           "_yuan": "app/money.py（唯一实现）", "_pct": "整数百分比", "_count": "本页条数"}
    for suf, label in UNIT_SUFFIX.items():
        lines.append("| " + suf + " | " + label + " | " + who.get(suf, "—") + " |")
    lines += ["", "## 1. 表清单", "", "| 表 | 行数 | 列数 | 写它的地方 |", "|---|---|---|---|"]
    for t in tables:
        w = "<br>".join(t["writers"][:2]) or "—"
        lines.append("| " + t["name"] + " | " + str(t["row_count"]) + " | " +
                     str(len(t["columns"])) + " | " + w + " |")
    lines += ["", "## 2. 每张表的列", ""]
    for t in tables:
        lines += ["### " + t["name"], ""]
        lines.append("| 列 | 声明类型 | 非空 | 主键 | 单位/说明 | 枚举值 |")
        lines.append("|---|---|---|---|---|---|")
        for c in t["columns"]:
            enum = "、".join(str(x) for x in t["enums"].get(c["name"], []))
            lines.append("| " + c["name"] + " | " + (c["type"] or "—") + " | " +
                         ("是" if c["notnull"] else "") + " | " + ("是" if c["pk"] else "") + " | " +
                         unit_of(c) + " | " + enum + " |")
        extra = []
        if t["indexes"]:
            extra.append("索引：" + "；".join(
                i["name"] + "(" + ",".join(i["cols"]) + ")" + (" UNIQUE" if i["unique"] else "")
                for i in t["indexes"]))
        if t["fks"]:
            extra.append("外键：" + "；".join(f["col"] + "→" + f["ref_table"] + "." + f["ref_col"] for f in t["fks"]))
        if t["triggers"]:
            extra.append("触发器：" + "、".join(t["triggers"]))
        if t["readers"]:
            extra.append("读它：" + "、".join(t["readers"][:4]))
        if extra:
            lines += [""] + [x + "  " for x in extra]
        lines.append("")
    lines += ["## 3. 接口清单", "",
              "| 方法 | 路径 | 分组 | 读/写 | 参数 | 请求体字段 | 采样 |", "|---|---|---|---|---|---|---|"]
    for op in ops:
        params = "、".join(
            p["name"] + "(" + p["in"] + "," + p["type"] + (",必填" if p["required"] else "") + ")"
            for p in op["params"]) or "—"
        body = "、".join(b["path"] + ("*" if b["required"] else "") for b in op["body"]) or "—"
        key = op["method"] + " " + op["path"]
        s = samples.get(key)
        st = str(s["status"]) if s else ("写端点未采样" if op["writes"] else "路径参数未采样")
        lines.append("| " + op["method"] + " | " + op["path"] + " | " + op["tag"] + " | " +
                     ("写" if op["writes"] else "读") + " | " + params + " | " + body + " | " + st + " |")
    lines += ["", "## 4. 请求体字段明细（含校验边界）", ""]
    for op in ops:
        if not op["body"]:
            continue
        lines += ["### " + op["method"] + " " + op["path"], "",
                  "| 字段 | 类型 | 必填 | 最小 | 最大 | 长度 | 枚举 | 默认 |", "|---|---|---|---|---|---|---|---|"]
        for b in op["body"]:
            lines.append("| " + b["path"] + " | " + b["type"] + " | " + ("是" if b["required"] else "") + " | " +
                         (str(b["minimum"]) if b["minimum"] is not None else "") + " | " +
                         (str(b["maximum"]) if b["maximum"] is not None else "") + " | " +
                         (str(b["maxLength"]) if b["maxLength"] else "") + " | " +
                         ("、".join(str(x) for x in b["enum"]) if b["enum"] else "") + " | " +
                         (str(b["default"]) if b["default"] is not None else "") + " |")
        lines.append("")
    lines += ["## 5. 只读端点真实响应形状（采样）", ""]
    for key, s in samples.items():
        if not s or s.get("status") != 200:
            continue
        lines += ["### " + key, "", FENCE + "json",
                  json.dumps(s["shape"], ensure_ascii=False, indent=1)[:4000], FENCE, ""]
    lines += ["## 6. 命名与单位违规（自动检查）", ""]
    violations = []
    for t in tables:
        for c in t["columns"]:
            n, typ = c["name"], (c["type"] or "").upper()
            if n.endswith("_fen") and "INT" not in typ:
                violations.append(t["name"] + "." + n + "：_fen 却不是 INTEGER（实际 " + (c["type"] or "无类型") + "）")
            if n.endswith("_ms") and "INT" not in typ:
                violations.append(t["name"] + "." + n + "：_ms 却不是 INTEGER")
            if n.endswith("_json") and "TEXT" not in typ:
                violations.append(t["name"] + "." + n + "：_json 却不是 TEXT")
            if ("amount" in n or "balance" in n or "budget" in n) and not any(
                    n.endswith(s) for s in ("_fen", "_yuan", "_pct", "_text", "_json")):
                violations.append(t["name"] + "." + n + "：金额类列没有单位后缀")
    lines += (["- " + v for v in violations] if violations
              else ["无违规：金额列一律 _fen、时间列一律 _ms、结构化列一律 _json。"])
    write_ops = [o for o in ops if o["writes"]]
    sampled_keys = set(samples)
    unsampled = len([o for o in write_ops
                     if (o["method"] + " " + o["path"]) not in sampled_keys])
    lines += ["", "## 7. 仍然不清楚的部分（结论）", "",
              "- 计数：所有 count 都是本页条数，接口没有 total（见分层接口数据核对 §4）。",
              "- 枚举：状态值域由 CHECK 约束与代码常量两处维护，字典只能看到出现过的值。",
              "- 时间：毫秒列无时区列，一律按服务器本地时区解释。",
              "- 写端点：" + str(unsampled) + " 个写方法未采样（会在临时库上改数据），字段以 §4 的 schema 为准。",
              ""]
    notes = ROOT / "docs" / "audit" / "数据字典_结论.md"
    if notes.exists():
        lines += ["", notes.read_text(encoding="utf-8").rstrip(), ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "bank.db"))
    ap.add_argument("--json", default=str(ROOT / "tmp" / "data_map.json"))
    ap.add_argument("--md", default=str(ROOT / "docs" / "audit" / "数据字典.md"))
    ap.add_argument("--no-sample", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    tables = db_section(conn)
    conn.close()

    from app.main import app

    spec = app.openapi()
    ops = api_section(spec)

    payload = {"tables": tables, "ops": ops, "samples": {}}
    if not args.no_sample:
        payload["samples"] = sample_responses(ops, Path(args.db))

    Path(args.json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    md = render_md(payload, spec)
    Path(args.md).parent.mkdir(parents=True, exist_ok=True)
    Path(args.md).write_text(md, encoding="utf-8")
    print("tables=" + str(len(tables)) + " ops=" + str(len(ops)) + " samples=" + str(len(payload["samples"])))
    print("json -> " + args.json)
    print("md   -> " + args.md + " (" + str(md.count("\n")) + " 行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

