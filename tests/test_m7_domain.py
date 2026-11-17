import pytest

import app.store as store
from app.modules.m7_acceptance import repository as repo
from app.modules.m7_acceptance.domain import check_pages, diff_checks, normalize_page_list
from app.modules.m7_acceptance.hashing import (
    plan_digest,
    recheck_id,
    scope_digest,
    seal_id,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "m7.sqlite3")
    repo.init_m7()
    return tmp_path


HEADER = "issue,edition,page,file_name,sha256,phash,clarity,header_issue\n"


def scan_row(issue="2026-11-01", edition="01", page="001", name="a.tif"):
    return f"{issue},{edition},{page},{name},{'a' * 64},0f0f,90,{issue}\n"


def manifest(pages, issue="2026-11-01", edition="01"):
    body = HEADER
    for index, page in enumerate(pages):
        body += f"{issue},{edition},{int(page):03d},scan_{page}.tif,{'b' * 63}{index:x},0f0f,88,{issue}\n"
    return body


def test_check_pages_reports_expected_actual_missing_and_out_of_range():
    result = check_pages(["001", "002", "005"], ["001", "002", "003", "004"])
    assert result["expected_count"] == 4
    assert result["actual_pages"] == [1, 2, 5]
    assert result["missing_pages"] == [3, 4]
    assert result["out_of_range_pages"] == [5]
    assert result["conclusion"] == "FAIL"


def test_complete_pages_pass_and_duplicates_flagged():
    result = check_pages(["001", "001", "002"], ["001", "002"])
    assert result["conclusion"] == "PASS"
    assert result["duplicate_pages"] == [1]
    assert result["actual_count"] == 2


def test_plan_page_list_rejects_duplicates_and_empty():
    assert normalize_page_list([1, "2", "003"]) == ["001", "002", "003"]
    with pytest.raises(ValueError, match="DUPLICATE_PLAN_PAGE"):
        normalize_page_list([1, 1])
    with pytest.raises(ValueError, match="EMPTY_PAGE_PLAN"):
        normalize_page_list([])


def test_diff_between_old_and_revised_plan():
    baseline = check_pages(["001", "002", "004"], ["001", "002", "003", "004"])
    revised = check_pages(["001", "002", "004"], ["001", "002", "004"])
    delta = diff_checks(baseline, revised)
    assert delta["cleared_missing_pages"] == [3]
    assert delta["conclusion_changed"] is True
    assert delta["baseline_conclusion"] == "FAIL" and delta["current_conclusion"] == "PASS"


def test_digests_are_deterministic_and_distinct():
    d1 = plan_digest("2026-11-01|01", 1, ["001"], "函-1")
    d2 = plan_digest("2026-11-01|01", 2, ["001", "002"], "函-2")
    assert d1 == plan_digest("2026-11-01|01", 1, ["001"], "函-1")
    assert d1 != d2
    rows = [{"page": "001", "file_name": "a.tif", "sha256": "x", "version": 1, "clarity": 9}]
    assert scope_digest("k", rows) == scope_digest("k", list(reversed(rows)))
    sid = seal_id("k", 1, d1, scope_digest("k", rows))
    assert sid == seal_id("k", 1, d1, scope_digest("k", rows))
    assert recheck_id(sid, 2) == recheck_id(sid, 2)
