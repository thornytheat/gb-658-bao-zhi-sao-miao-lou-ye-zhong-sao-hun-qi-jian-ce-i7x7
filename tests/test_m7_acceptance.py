import pytest
from fastapi.testclient import TestClient

import app.store as store
from app.main import app
from app.modules.m7_acceptance import repository as repo
from app.modules.m7_acceptance import service

client = TestClient(app)

HEADER = "issue,edition,page,file_name,sha256,phash,clarity,header_issue\n"


def manifest(pages, issue="2026-11-01", edition="01", extra_bad=False):
    body = HEADER
    for index, page in enumerate(pages):
        body += (
            f"{issue},{edition},{int(page):03d},scan_{int(page):03d}.tif,"
            f"{'b' * 63}{index:x},0f0f,88,{issue}\n"
        )
    if extra_bad:
        body += f"{issue},{edition},009,broken.tif,abc\n"  # 列不齐 → 隔离
    return body


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "m7.sqlite3")
    repo.init_m7()
    yield


def import_scans(pages, **kwargs):
    response = client.post(
        "/api/acceptance/scans/import",
        json={"text": manifest(pages, **kwargs), "source_name": "daily.csv"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def publish(pages, basis="报社勘误函 2026-07", expected_version=None):
    payload = {
        "issue": "2026-11-01",
        "edition": "01",
        "expected_pages": pages,
        "basis": basis,
    }
    if expected_version is not None:
        payload["expected_version"] = expected_version
    response = client.post("/api/acceptance/plans", json=payload)
    return response


def test_import_real_manifest_quarantines_bad_rows_and_is_idempotent():
    first = import_scans([1, 2, 4], extra_bad=True)
    assert first["accepted"] == 3 and first["quarantined"] == 1
    assert first["quarantine_evidence"][0]["reason_code"] == "COLUMN_COUNT"
    second = client.post(
        "/api/acceptance/scans/import",
        json={"text": manifest([1, 2, 4], extra_bad=True)},
    ).json()
    assert second["reused"] is True and second["import_id"] == first["import_id"]
    assert len(client.get("/api/acceptance/scans/imports").json()["imports"]) == 1


def test_plan_requires_basis_and_versions_are_immutable():
    response = client.post(
        "/api/acceptance/plans",
        json={"issue": "2026-11-01", "edition": "1", "expected_pages": [1, 2, 3, 4], "basis": "  "},
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "BASIS_REQUIRED"

    v1 = publish([1, 2, 3, 4], basis="初版排期单").json()
    assert v1["version"] == 1 and v1["issue_edition"] == "2026-11-01|01"
    v2 = publish([1, 2, 4], basis="更正稿：第3版撤版").json()
    assert v2["version"] == 2 and len(v2["expected_pages"]) == 3
    # 旧版本仍可回读，计划发布只追加不改写
    old = client.get("/api/acceptance/plans", params={"issue": "2026-11-01", "edition": "01", "version": 1}).json()
    assert old["expected_pages"] == ["001", "002", "003", "004"]


def test_stale_publish_with_old_expected_version_is_rejected():
    publish([1, 2, 3], basis="v1")
    response = publish([1, 2], basis="陈旧客户端", expected_version=1)
    assert response.status_code == 409 and response.json()["error"]["code"] == "PLAN_VERSION_CONFLICT"


def test_seal_freezes_plan_and_scope_and_keeps_conclusion_on_later_revision():
    import_scans([1, 2, 4])
    publish([1, 2, 3, 4], basis="初版排期单")
    seal = client.post(
        "/api/acceptance/seals",
        json={"issue": "2026-11-01", "edition": "01", "plan_version": 1},
    ).json()
    assert seal["created"] is True
    assert seal["conclusion"] == "FAIL"
    assert seal["check_result"]["missing_pages"] == [3]
    assert seal["check_result"]["out_of_range_pages"] == []
    frozen_scope = seal["scope_digest"]
    frozen_plan_digest = seal["plan_digest"]

    # 更正稿发布：应有页变为 1,2,4
    publish([1, 2, 4], basis="更正稿：第3版撤版")
    # 历史签章原件回读结论不变，仍是冻结时的 FAIL/缺第3页
    sealed_again = client.get(f"/api/acceptance/seals/{seal['seal_id']}").json()
    assert sealed_again["conclusion"] == "FAIL"
    assert sealed_again["plan_version"] == 1
    assert sealed_again["scope_digest"] == frozen_scope
    assert sealed_again["plan_digest"] == frozen_plan_digest

    # 同计划同范围重复签章：幂等复用，不生成第二份
    retry = client.post(
        "/api/acceptance/seals",
        json={"issue": "2026-11-01", "edition": "01", "plan_version": 1},
    ).json()
    assert retry["reused"] is True and retry["seal_id"] == seal["seal_id"]
    assert len(client.get("/api/acceptance/seals").json()["seals"]) == 1


def test_stale_seal_request_rejected_by_version_guard():
    import_scans([1, 2])
    publish([1, 2], basis="初版")
    response = client.post(
        "/api/acceptance/seals",
        json={"issue": "2026-11-01", "edition": "01", "plan_version": 1, "expected_plan_version": 9},
    )
    assert response.status_code == 409 and response.json()["error"]["code"] == "STALE_SEAL_REQUEST"


def test_recheck_against_revised_plan_saves_diff_without_overwriting_seal():
    import_scans([1, 2, 4])
    publish([1, 2, 3, 4], basis="初版排期单")
    seal = client.post(
        "/api/acceptance/seals",
        json={"issue": "2026-11-01", "edition": "01", "plan_version": 1},
    ).json()
    publish([1, 2, 4], basis="更正稿：第3版撤版")

    recheck = client.post(
        "/api/acceptance/rechecks", json={"seal_id": seal["seal_id"], "target_plan_version": 2}
    ).json()
    assert recheck["created"] is True
    assert recheck["conclusion"] == "PASS"
    assert recheck["diff"]["cleared_missing_pages"] == [3]
    assert recheck["diff"]["conclusion_changed"] is True
    assert recheck["scope_digest"] == seal["scope_digest"]

    # 原件未被覆盖
    original = client.get(f"/api/acceptance/seals/{seal['seal_id']}").json()
    assert original["conclusion"] == "FAIL" and original["plan_version"] == 1

    # 复验重试幂等
    again = client.post(
        "/api/acceptance/rechecks", json={"seal_id": seal["seal_id"], "target_plan_version": 2}
    ).json()
    assert again["reused"] is True and again["recheck_id"] == recheck["recheck_id"]
    listing = client.get("/api/acceptance/rechecks", params={"seal_id": seal["seal_id"]}).json()
    assert len(listing["rechecks"]) == 1
    detail = client.get(f"/api/acceptance/rechecks/{recheck['recheck_id']}").json()
    assert detail["diff"]["cleared_missing_pages"] == [3]


def test_recheck_detects_newly_missing_and_out_of_range_under_revision():
    import_scans([1, 2, 3])
    publish([1, 2, 3], basis="初版三版")
    seal = client.post(
        "/api/acceptance/seals",
        json={"issue": "2026-11-01", "edition": "01", "plan_version": 1},
    ).json()
    # 更正稿扩为四版：第4页缺失；扫描里的第3页仍在范围内，另造越界需新范围，这里验缺页
    publish([1, 2, 3, 4], basis="更正稿：恢复第4版")
    recheck = client.post(
        "/api/acceptance/rechecks", json={"seal_id": seal["seal_id"], "target_plan_version": 2}
    ).json()
    assert recheck["check_result"]["missing_pages"] == [4]
    assert recheck["diff"]["newly_missing_pages"] == [4]

    # 越界：更正稿收窄为两版，扫描第3页越界
    publish([1, 2], basis="更正稿：撤第3、4版")
    recheck2 = client.post(
        "/api/acceptance/rechecks", json={"seal_id": seal["seal_id"], "target_plan_version": 3}
    ).json()
    assert recheck2["check_result"]["out_of_range_pages"] == [3]
    assert recheck2["diff"]["newly_out_of_range_pages"] == [3]


def test_recheck_job_resumes_after_interruption_without_duplicate_results():
    # 两个期版各签章
    for edition, pages in (("01", [1, 2]), ("02", [1, 2, 3])):
        client.post(
            "/api/acceptance/scans/import",
            json={"text": manifest(pages, edition=edition)},
        )
        client.post(
            "/api/acceptance/plans",
            json={"issue": "2026-11-01", "edition": edition, "expected_pages": pages, "basis": "初版"},
        )
        client.post(
            "/api/acceptance/seals",
            json={"issue": "2026-11-01", "edition": edition, "plan_version": 1},
        )
        client.post(
            "/api/acceptance/plans",
            json={"issue": "2026-11-01", "edition": edition, "expected_pages": pages + [9],
                  "basis": "更正稿增补第9页"},
        )

    # 首批只处理 1 个期版，模拟中断
    first = client.post(
        "/api/acceptance/recheck-jobs", json={"target_plan_version": 2, "batch_size": 1}
    ).json()
    assert first["status"] == "INTERRUPTED" and first["processed"] == 1 and first["total"] == 2
    job_id = first["job_id"]

    # 恢复：只处理剩余期版
    resumed = client.post(
        "/api/acceptance/recheck-jobs", json={"target_plan_version": 2, "batch_size": 1}
    ).json()
    assert resumed["job_id"] == job_id and resumed["status"] == "DONE" and resumed["processed"] == 2

    # 再次重试不重复生成：复验总数仍为 2，结果标记 reused
    rerun = client.post(
        "/api/acceptance/recheck-jobs", json={"target_plan_version": 2, "batch_size": 5}
    ).json()
    assert rerun["job_id"] == job_id and len(rerun["detail"]["results"]) == 2
    # 重试不重复生成：复验总数仍为 2，且结果标识与首轮一致
    first_ids = {item["recheck_id"] for item in resumed["detail"]["results"]}
    rerun_ids = {item["recheck_id"] for item in rerun["detail"]["results"]}
    assert first_ids == rerun_ids
    assert len(client.get("/api/acceptance/rechecks").json()["rechecks"]) == 2
    assert client.get(f"/api/acceptance/recheck-jobs/{job_id}").json()["status"] == "DONE"


def test_issue_edition_readback_reports_all_four_accounts():
    import_scans([1, 2, 4])
    publish([1, 2, 3, 4], basis="初版排期单")
    overview = client.get(
        "/api/acceptance/issue-edition", params={"issue": "2026-11-01", "edition": "01"}
    ).json()
    check = overview["live_check"]
    assert check["expected_count"] == 4
    assert check["actual_count"] == 3
    assert check["missing_pages"] == [3]
    assert check["out_of_range_pages"] == []
    assert overview["scan_scope"]["row_count"] == 3
    assert len(overview["plan_versions"]) == 1


def test_seal_without_scans_is_rejected_and_same_plan_recheck_rejected():
    publish([1, 2], basis="初版")
    response = client.post(
        "/api/acceptance/seals",
        json={"issue": "2026-11-01", "edition": "01", "plan_version": 1},
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "EMPTY_SCAN_SCOPE"


def test_acceptance_page_renders():
    response = client.get("/acceptance")
    assert response.status_code == 200 and "panel" in response.text


def test_service_layer_concurrency_guard_on_plan_insert(monkeypatch):
    service.import_scan_manifest(manifest([1, 2]), source_name="x.csv")
    service.publish_plan("2026-11-01", "01", [1, 2], "初版")
    # 模拟并发：强制下一个版本号仍为 2，但库中唯一约束拒绝重复
    monkeypatch.setattr(repo, "next_plan_version", lambda key: 1)
    with pytest.raises(Exception) as exc:
        service.publish_plan("2026-11-01", "01", [1, 2, 3], "并发重复版本")
    assert exc.value.code == "PLAN_VERSION_CONFLICT"
