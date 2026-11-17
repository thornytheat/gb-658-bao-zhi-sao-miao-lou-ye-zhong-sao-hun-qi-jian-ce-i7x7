"""M7 应用服务：计划发布、验收签章、复验与回读。

边界约束：
- 只读取真实扫描清单并做缺/越界核对；不做重扫裁决、不派工补扫。
- 签章冻结当时的计划版本与扫描范围摘要；历史签章原件不可变。
- 复验另存记录与差异，绝不覆盖签章。
"""
from app.errors import DomainError
from app.modules.m1_intake.parser import parse_manifest
from app.modules.m2_issue.rules import normalize_edition, normalize_issue

from . import repository as repo
from .domain import check_pages, diff_checks, issue_edition_key, normalize_page_list
from .hashing import (
    digest,
    plan_digest as compute_plan_digest,
    recheck_id,
    scope_digest as compute_scope_digest,
    seal_id,
)


def _err(code: str, message: str, status: int = 422) -> DomainError:
    return DomainError(code, message, status=status)


# ---------- 真实扫描清单接入 ----------

def import_scan_manifest(text: str, source_name: str = "upload.csv") -> dict:
    """采信真实扫描清单；坏行进隔离并保留物理行号，采纳行按期版归档。"""
    accepted_rows, quarantine = parse_manifest(text)
    input_sha = digest(text or "")
    import_id = "IMP-" + input_sha[:12]
    stored_rows = []
    for row in accepted_rows:
        issue = normalize_issue(row["issue"])
        edition = normalize_edition(row["edition"])
        page = normalize_page_list([row["page"]])[0]
        clarity = row.get("clarity")
        try:
            clarity = int(clarity) if str(clarity).strip() != "" else None
        except (TypeError, ValueError):
            clarity = None
        stored_rows.append(
            {
                "issue_edition": "{}|{}".format(issue, edition),
                "issue": issue,
                "edition": edition,
                "page": page,
                "file_name": row["file_name"].strip(),
                "sha256": row["sha256"].strip(),
                "phash": (row.get("phash") or "").strip() or None,
                "clarity": clarity,
                "header_issue": (row.get("header_issue") or "").strip() or None,
                "version": int(row.get("version") or 1),
                "line_no": row["line_no"],
            }
        )
    record = {
        "import_id": import_id,
        "source_name": source_name,
        "input_sha256": input_sha,
        "accepted_count": len(stored_rows),
        "quarantined_count": len(quarantine),
        "created_at": repo.now_iso(),
    }
    created = repo.insert_import(record, stored_rows, quarantine)
    return {
        "import_id": import_id,
        "created": created,
        "reused": not created,
        "source_name": source_name,
        "input_sha256": input_sha,
        "accepted": len(stored_rows),
        "quarantined": len(quarantine),
        "quarantine_evidence": quarantine,
        "issue_editions": sorted({r["issue_edition"] for r in stored_rows}),
    }


def read_import(import_id: str) -> dict:
    record = repo.get_import(import_id)
    if record is None:
        raise _err("IMPORT_NOT_FOUND", f"扫描清单 {import_id} 不存在", status=404)
    return record


def list_imports() -> list[dict]:
    return repo.list_imports()


# ---------- 带依据的页码计划，按期版版本化发布 ----------

def publish_plan(issue: str, edition: str, expected_pages: list, basis: str,
                 expected_version: int | None = None) -> dict:
    if not basis or not str(basis).strip():
        raise _err("BASIS_REQUIRED", "页码计划必须携带更正依据（文号/来函/审批说明）")
    key = issue_edition_key(issue, edition)
    pages = normalize_page_list(expected_pages)
    # 版本号先读、插入靠唯一约束兜底，并发发布只有一个成功
    next_version = repo.next_plan_version(key)
    if expected_version is not None and expected_version != next_version:
        # 陈旧客户端拿着旧版本号提交：拒绝，要求回读后重试
        raise _err(
            "PLAN_VERSION_CONFLICT",
            f"期版 {key} 最新版本为 {next_version - 1}，拒绝陈旧发布 {expected_version}",
            status=409,
        )
    plan = {
        "issue_edition": key,
        "version": next_version,
        "expected_pages": pages,
        "basis": str(basis).strip(),
        "plan_digest": compute_plan_digest(key, next_version, pages, str(basis).strip()),
        "published_at": repo.now_iso(),
    }
    created = repo.insert_plan(plan)
    if not created:
        raise _err(
            "PLAN_VERSION_CONFLICT",
            f"期版 {key} 版本 {next_version} 已存在，并发发布被拒绝",
            status=409,
        )
    return {"created": True, **plan}


def read_plan(issue: str, edition: str, version: int | None = None) -> dict:
    key = issue_edition_key(issue, edition)
    plan = repo.get_plan(key, version)
    if plan is None:
        raise _err("PLAN_NOT_FOUND", f"期版 {key} 没有已发布的页码计划", status=404)
    return plan


def list_plan_versions(issue: str, edition: str) -> list[dict]:
    return repo.list_plans(issue_edition_key(issue, edition))


# ---------- 验收签章：冻结计划与扫描范围 ----------

def seal_acceptance(issue: str, edition: str, plan_version: int,
                    expected_plan_version: int | None = None) -> dict:
    key = issue_edition_key(issue, edition)
    plan = repo.get_plan(key, plan_version)
    if plan is None:
        raise _err("PLAN_NOT_FOUND", f"期版 {key} 计划版本 {plan_version} 不存在", status=404)
    if expected_plan_version is not None and expected_plan_version != plan_version:
        raise _err(
            "STALE_SEAL_REQUEST",
            f"请求依据版本 {expected_plan_version} 与指定版本 {plan_version} 不一致，拒绝陈旧签章",
            status=409,
        )
    rows = repo.scope_rows(key)
    if not rows:
        raise _err("EMPTY_SCAN_SCOPE", f"期版 {key} 没有任何采纳扫描件，无法签章")
    frozen_scope = compute_scope_digest(key, rows)
    sid = seal_id(key, plan["version"], plan["plan_digest"], frozen_scope)
    existing = repo.get_seal(sid)
    if existing is not None:
        # 同一计划+同一扫描范围的签章是确定性的：重试复用，不重复生成
        return {"created": False, "reused": True, **existing}
    result = check_pages([r["page"] for r in rows], plan["expected_pages"])
    seal = {
        "seal_id": sid,
        "issue_edition": key,
        "plan_version": plan["version"],
        "plan_digest": plan["plan_digest"],
        "scope_digest": frozen_scope,
        "expected_pages": plan["expected_pages"],
        "check_result": result,
        "conclusion": result["conclusion"],
        "sealed_at": repo.now_iso(),
    }
    created = repo.insert_seal(seal)
    if not created:
        existing = repo.get_seal(sid)
        return {"created": False, "reused": True, **(existing or seal)}
    return {"created": True, "reused": False, **seal}


def read_seal(seal_id_value: str) -> dict:
    seal = repo.get_seal(seal_id_value)
    if seal is None:
        raise _err("SEAL_NOT_FOUND", f"签章 {seal_id_value} 不存在", status=404)
    return seal


def list_seals(issue: str | None = None, edition: str | None = None) -> list[dict]:
    key = issue_edition_key(issue, edition) if issue and edition else None
    return repo.list_seals(key)


# ---------- 历史批次按新稿复验，原件不动 ----------

def recheck_seal(seal_id_value: str, target_plan_version: int | None = None) -> dict:
    seal = repo.get_seal(seal_id_value)
    if seal is None:
        raise _err("SEAL_NOT_FOUND", f"签章 {seal_id_value} 不存在", status=404)
    key = seal["issue_edition"]
    target = repo.get_plan(key, target_plan_version)
    if target is None:
        raise _err("PLAN_NOT_FOUND", f"期版 {key} 复验目标计划不存在", status=404)
    if target["version"] == seal["plan_version"] and target["plan_digest"] == seal["plan_digest"]:
        raise _err(
            "SAME_PLAN_RECHECK",
            f"签章已基于计划版本 {target['version']}，无需同稿复验",
            status=422,
        )
    rid = recheck_id(seal_id_value, target["version"])
    existing = repo.get_recheck(rid)
    if existing is not None:
        # 复验中断后重试：同一签章+同一目标版本只生成一份结果
        return {"created": False, "reused": True, **existing}
    # 复验沿用签章冻结的扫描范围：从当前库重算摘要必须与冻结值一致
    rows = repo.scope_rows(key)
    current_scope = compute_scope_digest(key, rows)
    if current_scope != seal["scope_digest"]:
        raise _err(
            "SCOPE_DRIFT",
            f"期版 {key} 当前扫描范围与签章冻结范围不一致，拒绝在漂移范围上复验",
            status=409,
        )
    result = check_pages([r["page"] for r in rows], target["expected_pages"])
    delta = diff_checks(seal["check_result"], result)
    record = {
        "recheck_id": rid,
        "seal_id": seal_id_value,
        "issue_edition": key,
        "baseline_plan_version": seal["plan_version"],
        "target_plan_version": target["version"],
        "target_plan_digest": target["plan_digest"],
        "scope_digest": seal["scope_digest"],
        "check_result": result,
        "diff": delta,
        "conclusion": result["conclusion"],
        "created_at": repo.now_iso(),
    }
    created = repo.insert_recheck(record)
    if not created:
        existing = repo.get_recheck(rid)
        return {"created": False, "reused": True, **(existing or record)}
    return {"created": True, "reused": False, **record}


def read_recheck(recheck_id_value: str) -> dict:
    record = repo.get_recheck(recheck_id_value)
    if record is None:
        raise _err("RECHECK_NOT_FOUND", f"复验 {recheck_id_value} 不存在", status=404)
    return record


def list_rechecks(seal_id_value: str | None = None) -> list[dict]:
    return repo.list_rechecks(seal_id_value)


# ---------- 断点续跑：中断后恢复未完期版，重试不重复生成 ----------

def start_recheck_job(target_plan_version: int | None = None, batch_size: int = 10) -> dict:
    seals = repo.list_seals()
    if not seals:
        raise _err("NO_SEALED_BATCH", "当前没有已签章批次可复验", status=422)
    chosen_version = target_plan_version
    targets = []
    for seal in seals:
        key = seal["issue_edition"]
        target = repo.get_plan(key, target_plan_version)
        if target is None:
            continue
        if target["version"] == seal["plan_version"]:
            continue  # 同稿无需复验
        if chosen_version is None:
            chosen_version = target["version"]
        if target["version"] != chosen_version:
            continue
        targets.append(seal["seal_id"])
    if not targets:
        raise _err("NO_RECHECK_TARGET", "没有需要按新稿复验的签章批次", status=422)
    job_id = "JOB-" + digest("|".join([str(chosen_version)] + sorted(targets)))[:12]
    existing = repo.get_job(job_id)
    if existing is not None:
        return _resume_job(existing, batch_size)
    job = {
        "job_id": job_id,
        "target_plan_version": chosen_version,
        "status": "RUNNING",
        "total": len(targets),
        "processed": 0,
        "created_at": repo.now_iso(),
        "updated_at": repo.now_iso(),
        "detail": {"seal_ids": sorted(targets), "results": [], "errors": []},
    }
    repo.upsert_job(job)
    return _resume_job(repo.get_job(job_id), batch_size)


def _resume_job(job: dict, batch_size: int) -> dict:
    """每轮只处理剩余任务；已生成的复验结果通过 recheck_id 幂等复用。"""
    done_ids = {item["seal_id"] for item in job["detail"]["results"]}
    pending = [sid for sid in job["detail"]["seal_ids"] if sid not in done_ids
               and sid not in {e["seal_id"] for e in job["detail"]["errors"]}]
    for sid in pending[: max(1, batch_size)]:
        try:
            record = recheck_seal(sid, job["target_plan_version"])
            job["detail"]["results"].append(
                {"seal_id": sid, "recheck_id": record["recheck_id"], "created": record["created"]}
            )
        except DomainError as exc:
            job["detail"]["errors"].append({"seal_id": sid, "code": exc.code, "message": exc.message})
        job["processed"] += 1
    job["status"] = "DONE" if job["processed"] >= job["total"] else "INTERRUPTED"
    job["updated_at"] = repo.now_iso()
    repo.upsert_job(job)
    return repo.get_job(job["job_id"])


def read_job(job_id: str) -> dict:
    job = repo.get_job(job_id)
    if job is None:
        raise _err("JOB_NOT_FOUND", f"复验任务 {job_id} 不存在", status=404)
    return job


def read_issue_edition(issue: str, edition: str) -> dict:
    """回读接口：一期版的计划版本、扫描范围、签章与复验全账。"""
    key = issue_edition_key(issue, edition)
    rows = repo.scope_rows(key)
    plan = repo.get_plan(key)
    overview = {
        "issue_edition": key,
        "latest_plan": plan,
        "plan_versions": repo.list_plans(key),
        "scan_scope": {
            "row_count": len(rows),
            "scope_digest": compute_scope_digest(key, rows) if rows else None,
            "pages": sorted({r["page"] for r in rows}, key=int),
        },
        "seals": repo.list_seals(key),
    }
    if plan and rows:
        overview["live_check"] = check_pages([r["page"] for r in rows], plan["expected_pages"])
    return overview
