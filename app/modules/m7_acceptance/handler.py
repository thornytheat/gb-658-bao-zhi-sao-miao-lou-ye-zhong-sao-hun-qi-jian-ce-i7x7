from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.web import page
from . import service

router = APIRouter(prefix="/api/acceptance", tags=["m7-acceptance"])
page_router = APIRouter(tags=["m7-acceptance-view"])


class PlanPublishIn(BaseModel):
    issue: str
    edition: str
    expected_pages: list[int | str] = Field(min_length=1)
    basis: str
    expected_version: int | None = None


class SealIn(BaseModel):
    issue: str
    edition: str
    plan_version: int
    expected_plan_version: int | None = None


class RecheckIn(BaseModel):
    seal_id: str
    target_plan_version: int | None = None


class RecheckJobIn(BaseModel):
    target_plan_version: int | None = None
    batch_size: int = 10


@page_router.get("/acceptance", response_class=HTMLResponse)
def view(request: Request):
    rows = [
        {"key": "2026-11-01|01", "status": "计划 v2 已发布", "value": "依据：勘误函 2026-07"},
        {"key": "2026-11-01|01", "status": "历史签章冻结 v1", "value": "SEAL 原件保留"},
        {"key": "2026-11-01|01", "status": "按 v2 复验差异", "value": "缺页/越界可回读"},
    ]
    return page(request, "m7.html", "验收签章与修订复验", "/api/acceptance/plans", rows)


# ---------- 扫描清单 ----------

@router.post("/scans/import")
def import_scans(payload: dict):
    text = payload.get("text") or payload.get("content") or ""
    source = payload.get("source_name") or "upload.csv"
    return service.import_scan_manifest(text, source)


@router.get("/scans/imports")
def get_imports():
    return {"imports": service.list_imports()}


@router.get("/scans/imports/{import_id}")
def get_import(import_id: str):
    return service.read_import(import_id)


# ---------- 计划发布与回读 ----------

@router.post("/plans")
def publish_plan(payload: PlanPublishIn):
    return service.publish_plan(
        payload.issue,
        payload.edition,
        payload.expected_pages,
        payload.basis,
        payload.expected_version,
    )


@router.get("/plans")
def read_plan(issue: str, edition: str, version: int | None = None):
    return service.read_plan(issue, edition, version)


@router.get("/plans/versions")
def plan_versions(issue: str, edition: str):
    return {"issue_edition": f"{issue}|{edition}", "versions": service.list_plan_versions(issue, edition)}


# ---------- 验收签章与回读 ----------

@router.post("/seals")
def seal(payload: SealIn):
    return service.seal_acceptance(
        payload.issue, payload.edition, payload.plan_version, payload.expected_plan_version
    )


@router.get("/seals")
def seals(issue: str | None = None, edition: str | None = None):
    return {"seals": service.list_seals(issue, edition)}


@router.get("/seals/{seal_id}")
def get_seal(seal_id: str):
    return service.read_seal(seal_id)


# ---------- 复验、断点续跑与回读 ----------

@router.post("/rechecks")
def recheck(payload: RecheckIn):
    return service.recheck_seal(payload.seal_id, payload.target_plan_version)


@router.get("/rechecks")
def get_rechecks(seal_id: str | None = None):
    return {"rechecks": service.list_rechecks(seal_id)}


@router.get("/rechecks/{recheck_id}")
def get_recheck(recheck_id: str):
    return service.read_recheck(recheck_id)


@router.post("/recheck-jobs")
def start_job(payload: RecheckJobIn):
    return service.start_recheck_job(payload.target_plan_version, payload.batch_size)


@router.get("/recheck-jobs/{job_id}")
def get_job(job_id: str):
    return service.read_job(job_id)


# ---------- 期版全账回读 ----------

@router.get("/issue-edition")
def issue_edition_overview(issue: str, edition: str):
    return service.read_issue_edition(issue, edition)
