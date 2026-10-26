"""文档数字单一事实源（W6）：文档里的数字必须与 `release_manifest.json` 一致。

这里测的是 `scripts/check_docs_numbers.py` 这个「文档数字校验器」本身，不是测某个业务功能：

  1. 仓内现在的文档全部与清单一致（不一致就红，修文档或重生成清单，不许改脚本放宽）；
  2. 写错的数字能被抓到（往临时文档里塞 9999 passed，必须报出来且期望值是清单里的数）；
  3. 带历史标记的行放行（「历史基线 904 passed」是那一轮的证据，不许被要求改成今天）；
  4. 不等的比值不算通过数（「红队 39/43」是部分通过，不是「43/43」那种结论）；
  5. 前端检查项数从 `scripts/check_frontend.py` 现取，不在文档里再抄一遍。
"""

from __future__ import annotations

import json

from scripts import check_docs_numbers as cdn


def _problem_labels(problems):
    return [label for _where, _expect, label, _text in problems]


def test_every_doc_number_matches_the_manifest():
    """仓内文档全绿：这条一红，说明有人改了代码没同步文档，或者文档写了清单里没有的数。"""
    problems = cdn.check()
    assert problems == [], "文档数字与 release_manifest.json 对不上：\n" + "\n".join(
        f"{where}  {label}应为 {expect}  ｜ {text}" for where, expect, label, text in problems
    )


def test_claims_cover_every_fact_we_publish():
    """规则集必须覆盖清单里对外公布的每个数，漏一条就等于少一道门。"""
    manifest = json.loads(cdn.MANIFEST.read_text(encoding="utf-8"))
    keys = {claim.key for claim in cdn.build_claims(manifest)}
    assert keys == {"pytest", "skipped", "tools", "tables", "events", "paths", "redteam", "e2e", "frontend"}


def test_a_wrong_test_count_is_caught(tmp_path):
    """写错的总数必须被指出来，而且期望值是清单里的数。"""
    doc = tmp_path / "wrong.md"
    doc.write_text("全量回归 9999 passed / 4 skipped；47 条 API 路径。\n", encoding="utf-8")
    problems = cdn.check(docs=[doc])
    labels = _problem_labels(problems)
    assert "pytest 通过数" in labels
    # 路径数这一行写对了，不应被连坐
    assert "API 路径数" not in labels
    expected = {label: expect for _where, expect, label, _text in problems}
    manifest = json.loads(cdn.MANIFEST.read_text(encoding="utf-8"))
    assert expected["pytest 通过数"] == manifest["tests"]["passed"]


def test_a_wrong_table_count_is_caught(tmp_path):
    """「30 张表」这种过时总数要被抓到（docs/15 那张改造前后对比表就踩过）。"""
    doc = tmp_path / "stale.md"
    doc.write_text("现在一共 30 张表。\n", encoding="utf-8")
    assert "数据库表数" in _problem_labels(cdn.check(docs=[doc]))


def test_history_marked_lines_are_skipped(tmp_path):
    """过去某一轮的数带「历史/基线/当时」标记时整行放行——那是证据，不是口误。"""
    doc = tmp_path / "history.md"
    doc.write_text(
        "历史基线：904 passed / 4 skipped，18 张表。\n"
        "当时红队 39/43，本轮 43/43。\n",
        encoding="utf-8",
    )
    assert cdn.check(docs=[doc]) == []


def test_unequal_ratios_are_not_treated_as_pass_counts(tmp_path):
    """「红队 39/43」是部分通过，不该被当成「红队 39/39」这种结论去校验。"""
    doc = tmp_path / "partial.md"
    doc.write_text("红队 39/43 通过。\n", encoding="utf-8")
    assert cdn.check(docs=[doc]) == []


def test_equal_ratios_must_match_the_manifest(tmp_path):
    """相等的比值才当结论：写错就要红。"""
    doc = tmp_path / "ratio.md"
    doc.write_text("红队 40/40 PASS。\n", encoding="utf-8")
    assert "红队通过数" in _problem_labels(cdn.check(docs=[doc]))


def test_frontend_check_count_is_read_from_the_gate():
    """前端检查项数不许抄：必须与 check_frontend.py 的实测一致。"""
    assert cdn.frontend_checks() >= 60


def test_one_correct_number_on_the_line_is_enough(tmp_path):
    """同一行同时写本轮与旧值时，只要有正确值就放行。"""
    manifest = json.loads(cdn.MANIFEST.read_text(encoding="utf-8"))
    passed = manifest["tests"]["passed"]
    doc = tmp_path / "both.md"
    doc.write_text(f"本轮 {passed} passed（改造前 904 passed）。\n", encoding="utf-8")
    assert cdn.check(docs=[doc]) == []
