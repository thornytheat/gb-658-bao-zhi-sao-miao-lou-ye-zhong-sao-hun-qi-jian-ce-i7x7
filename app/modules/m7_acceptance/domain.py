"""验收签章领域规则：期版键规范化、页码核对与差异比对。

本模块只做只读计算，不触碰数据库，保证规则可独立测试。
"""
import re

from app.modules.m2_issue.rules import normalize_edition, normalize_issue, normalize_page

PAGE_RE = re.compile(r"^\d+$")
PASS = "PASS"
FAIL = "FAIL"


def issue_edition_key(issue: str, edition: str) -> str:
    """规范化期版业务键（字符串存储，不做数字往返）。"""
    return "{}|{}".format(normalize_issue(issue), normalize_edition(edition))


def split_key(key: str) -> tuple[str, str]:
    parts = key.split("|")
    if len(parts) != 2:
        raise ValueError("BAD_ISSUE_EDITION_KEY")
    return parts[0], parts[1]


def normalize_page_list(pages) -> list[str]:
    """页码列表去重保序并规范为三位字符串；空列表非法。"""
    if not pages:
        raise ValueError("EMPTY_PAGE_PLAN")
    seen, result = set(), []
    for value in pages:
        page = normalize_page(str(value))
        if page in seen:
            raise ValueError("DUPLICATE_PLAN_PAGE")
        seen.add(page)
        result.append(page)
    return result


def page_numbers(pages: list[str]) -> list[int]:
    return [int(page) for page in pages]


def check_pages(actual_pages: list[str], expected_pages: list[str]) -> dict:
    """核对应有/实有/缺失/越界/重页。

    - expected：计划应有页（去重后的计划）
    - actual：扫描范围内采纳页（重页先折叠为一页，重复信息单独报告）
    - 越界 = 实有但不在应有集合内（含计划区间之外的任意页号）
    """
    expected_set = set(expected_pages)
    actual_counts: dict[str, int] = {}
    for page in actual_pages:
        actual_counts[page] = actual_counts.get(page, 0) + 1
    actual_set = set(actual_counts)
    missing = sorted(page_numbers(expected_set - actual_set))
    out_of_range = sorted(page_numbers(actual_set - expected_set))
    duplicates = sorted(page_numbers(page for page, count in actual_counts.items() if count > 1))
    present = sorted(page_numbers(actual_set & expected_set))
    conclusion = PASS if not missing and not out_of_range else FAIL
    return {
        "expected_pages": sorted(page_numbers(expected_set)),
        "actual_pages": sorted(page_numbers(actual_set)),
        "present_pages": present,
        "missing_pages": missing,
        "out_of_range_pages": out_of_range,
        "duplicate_pages": duplicates,
        "expected_count": len(expected_set),
        "actual_count": len(actual_set),
        "missing_count": len(missing),
        "out_of_range_count": len(out_of_range),
        "duplicate_count": len(duplicates),
        "conclusion": conclusion,
    }


def diff_checks(baseline: dict, current: dict) -> dict:
    """两份核对结论之间的差异；复验原件不被覆盖，差异单独成账。"""
    def delta(field: str) -> list[str]:
        return sorted(set(current[field]) ^ set(baseline[field]), key=int)

    newly_missing = sorted(set(current["missing_pages"]) - set(baseline["missing_pages"]), key=int)
    cleared_missing = sorted(set(baseline["missing_pages"]) - set(current["missing_pages"]), key=int)
    newly_out_of_range = sorted(
        set(current["out_of_range_pages"]) - set(baseline["out_of_range_pages"]), key=int
    )
    cleared_out_of_range = sorted(
        set(baseline["out_of_range_pages"]) - set(current["out_of_range_pages"]), key=int
    )
    return {
        "baseline_conclusion": baseline["conclusion"],
        "current_conclusion": current["conclusion"],
        "conclusion_changed": baseline["conclusion"] != current["conclusion"],
        "expected_changed": delta("expected_pages"),
        "actual_changed": delta("actual_pages"),
        "newly_missing_pages": newly_missing,
        "cleared_missing_pages": cleared_missing,
        "newly_out_of_range_pages": newly_out_of_range,
        "cleared_out_of_range_pages": cleared_out_of_range,
        "missing_count_delta": current["missing_count"] - baseline["missing_count"],
        "out_of_range_count_delta": current["out_of_range_count"] - baseline["out_of_range_count"],
    }
