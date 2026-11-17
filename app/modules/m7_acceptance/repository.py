"""M7 持久层：计划版本、扫描清单、签章冻结、复验与断点续跑任务。

所有写操作在短事务内完成；标识列带唯一约束，重复提交走 INSERT OR IGNORE，
保证重试不产生重复结果。连接沿用 app.store 的 DATABASE_PATH 配置。
"""
import json
import sqlite3

from app.errors import DomainError
from app.store import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS m7_scan_imports (
    import_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    accepted_count INTEGER NOT NULL,
    quarantined_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m7_scan_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id TEXT NOT NULL REFERENCES m7_scan_imports(import_id),
    issue_edition TEXT NOT NULL,
    issue TEXT NOT NULL,
    edition TEXT NOT NULL,
    page TEXT NOT NULL,
    file_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    phash TEXT,
    clarity INTEGER,
    header_issue TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    line_no INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS m7_scan_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id TEXT NOT NULL REFERENCES m7_scan_imports(import_id),
    line_no INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    raw TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m7_plans (
    issue_edition TEXT NOT NULL,
    version INTEGER NOT NULL,
    expected_pages TEXT NOT NULL,
    basis TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (issue_edition, version)
);
CREATE TABLE IF NOT EXISTS m7_seals (
    seal_id TEXT PRIMARY KEY,
    issue_edition TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    plan_digest TEXT NOT NULL,
    scope_digest TEXT NOT NULL,
    expected_pages TEXT NOT NULL,
    check_result TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    UNIQUE (issue_edition, plan_version, scope_digest)
);
CREATE TABLE IF NOT EXISTS m7_rechecks (
    recheck_id TEXT PRIMARY KEY,
    seal_id TEXT NOT NULL,
    issue_edition TEXT NOT NULL,
    baseline_plan_version INTEGER NOT NULL,
    target_plan_version INTEGER NOT NULL,
    target_plan_digest TEXT NOT NULL,
    scope_digest TEXT NOT NULL,
    check_result TEXT NOT NULL,
    diff TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m7_recheck_jobs (
    job_id TEXT PRIMARY KEY,
    target_plan_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL,
    processed INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


def init_m7() -> None:
    with connect() as db:
        db.executescript(SCHEMA)


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------- 扫描清单 ----------

def insert_import(record: dict, rows: list[dict], quarantine: list[dict]) -> bool:
    init_m7()
    with connect() as db:
        existing = db.execute(
            "SELECT 1 FROM m7_scan_imports WHERE import_id = ?", (record["import_id"],)
        ).fetchone()
        if existing:
            return False
        db.execute(
            "INSERT INTO m7_scan_imports VALUES (?, ?, ?, ?, ?, ?)",
            (
                record["import_id"],
                record["source_name"],
                record["input_sha256"],
                record["accepted_count"],
                record["quarantined_count"],
                record["created_at"],
            ),
        )
        db.executemany(
            """INSERT INTO m7_scan_rows
               (import_id, issue_edition, issue, edition, page, file_name, sha256,
                phash, clarity, header_issue, version, line_no)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    record["import_id"],
                    r["issue_edition"],
                    r["issue"],
                    r["edition"],
                    r["page"],
                    r["file_name"],
                    r["sha256"],
                    r.get("phash"),
                    r.get("clarity"),
                    r.get("header_issue"),
                    int(r.get("version", 1)),
                    r["line_no"],
                )
                for r in rows
            ],
        )
        db.executemany(
            "INSERT INTO m7_scan_quarantine (import_id, line_no, reason_code, raw) VALUES (?, ?, ?, ?)",
            [(record["import_id"], q["line_no"], q["reason_code"], q["raw"]) for q in quarantine],
        )
        return True


def list_imports() -> list[dict]:
    init_m7()
    with connect() as db:
        return [
            dict(zip(("import_id", "source_name", "input_sha256", "accepted_count",
                      "quarantined_count", "created_at"), row))
            for row in db.execute(
                "SELECT import_id, source_name, input_sha256, accepted_count, "
                "quarantined_count, created_at FROM m7_scan_imports ORDER BY created_at"
            ).fetchall()
        ]


def get_import(import_id: str) -> dict | None:
    init_m7()
    with connect() as db:
        row = db.execute(
            "SELECT import_id, source_name, input_sha256, accepted_count, "
            "quarantined_count, created_at FROM m7_scan_imports WHERE import_id = ?",
            (import_id,),
        ).fetchone()
        if not row:
            return None
        record = dict(zip(("import_id", "source_name", "input_sha256", "accepted_count",
                           "quarantined_count", "created_at"), row))
        record["quarantine"] = [
            dict(zip(("line_no", "reason_code", "raw"), item))
            for item in db.execute(
                "SELECT line_no, reason_code, raw FROM m7_scan_quarantine "
                "WHERE import_id = ? ORDER BY line_no",
                (import_id,),
            ).fetchall()
        ]
        return record


def scope_rows(issue_edition: str) -> list[dict]:
    """某期版当前全部采纳扫描行（同一文件可能多次导入，按文件去重）。"""
    init_m7()
    with connect() as db:
        rows = [
            dict(zip(("page", "file_name", "sha256", "phash", "clarity",
                      "header_issue", "version", "line_no"), item))
            for item in db.execute(
                """SELECT page, file_name, sha256, phash, clarity, header_issue, version, line_no
                   FROM m7_scan_rows WHERE issue_edition = ?
                   ORDER BY page, file_name""",
                (issue_edition,),
            ).fetchall()
        ]
    deduped: dict[tuple[str, str], dict] = {}
    for row in rows:
        deduped.setdefault((row["page"], row["file_name"]), row)
    return list(deduped.values())


def distinct_issue_editions() -> list[str]:
    init_m7()
    with connect() as db:
        return [r[0] for r in db.execute(
            "SELECT DISTINCT issue_edition FROM m7_scan_rows ORDER BY issue_edition"
        ).fetchall()]


# ---------- 页码计划（版本化，发布即不可变） ----------

def latest_plan_version(db: sqlite3.Connection, issue_edition: str) -> int:
    row = db.execute(
        "SELECT MAX(version) FROM m7_plans WHERE issue_edition = ?", (issue_edition,)
    ).fetchone()
    return row[0] or 0


def next_plan_version(issue_edition: str) -> int:
    init_m7()
    with connect() as db:
        return latest_plan_version(db, issue_edition) + 1


def insert_plan(plan: dict) -> bool:
    init_m7()
    with connect() as db:
        try:
            db.execute(
                "INSERT INTO m7_plans VALUES (?, ?, ?, ?, ?, ?)",
                (
                    plan["issue_edition"],
                    plan["version"],
                    json.dumps(plan["expected_pages"], ensure_ascii=False),
                    plan["basis"],
                    plan["plan_digest"],
                    plan["published_at"],
                ),
            )
            return True
        except sqlite3.IntegrityError:
            # 并发下同一期版同版本只允许一次发布，陈旧/重复操作被拒绝
            return False


def get_plan(issue_edition: str, version: int | None = None) -> dict | None:
    init_m7()
    with connect() as db:
        if version is None:
            version = latest_plan_version(db, issue_edition)
        if version == 0:
            return None
        row = db.execute(
            "SELECT issue_edition, version, expected_pages, basis, plan_digest, published_at "
            "FROM m7_plans WHERE issue_edition = ? AND version = ?",
            (issue_edition, version),
        ).fetchone()
    if not row:
        return None
    return {
        "issue_edition": row[0],
        "version": row[1],
        "expected_pages": json.loads(row[2]),
        "basis": row[3],
        "plan_digest": row[4],
        "published_at": row[5],
    }


def list_plans(issue_edition: str) -> list[dict]:
    init_m7()
    with connect() as db:
        rows = db.execute(
            "SELECT issue_edition, version, expected_pages, basis, plan_digest, published_at "
            "FROM m7_plans WHERE issue_edition = ? ORDER BY version",
            (issue_edition,),
        ).fetchall()
    return [
        {
            "issue_edition": r[0],
            "version": r[1],
            "expected_pages": json.loads(r[2]),
            "basis": r[3],
            "plan_digest": r[4],
            "published_at": r[5],
        }
        for r in rows
    ]


# ---------- 验收签章（冻结计划与扫描范围） ----------

def insert_seal(seal: dict) -> bool:
    init_m7()
    with connect() as db:
        try:
            db.execute(
                """INSERT INTO m7_seals
                   (seal_id, issue_edition, plan_version, plan_digest, scope_digest,
                    expected_pages, check_result, conclusion, sealed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    seal["seal_id"],
                    seal["issue_edition"],
                    seal["plan_version"],
                    seal["plan_digest"],
                    seal["scope_digest"],
                    json.dumps(seal["expected_pages"], ensure_ascii=False),
                    json.dumps(seal["check_result"], ensure_ascii=False),
                    seal["conclusion"],
                    seal["sealed_at"],
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_seal(seal_id_value: str) -> dict | None:
    init_m7()
    with connect() as db:
        row = db.execute("SELECT * FROM m7_seals WHERE seal_id = ?", (seal_id_value,)).fetchone()
    if not row:
        return None
    cols = ("seal_id", "issue_edition", "plan_version", "plan_digest", "scope_digest",
            "expected_pages", "check_result", "conclusion", "sealed_at")
    result = dict(zip(cols, row))
    result["expected_pages"] = json.loads(result["expected_pages"])
    result["check_result"] = json.loads(result["check_result"])
    return result


def list_seals(issue_edition: str | None = None) -> list[dict]:
    init_m7()
    with connect() as db:
        if issue_edition:
            rows = db.execute(
                "SELECT seal_id, issue_edition, plan_version, scope_digest, conclusion, sealed_at "
                "FROM m7_seals WHERE issue_edition = ? ORDER BY sealed_at",
                (issue_edition,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT seal_id, issue_edition, plan_version, scope_digest, conclusion, sealed_at "
                "FROM m7_seals ORDER BY sealed_at"
            ).fetchall()
    return [
        dict(zip(("seal_id", "issue_edition", "plan_version", "scope_digest",
                  "conclusion", "sealed_at"), r))
        for r in rows
    ]


# ---------- 复验（不覆盖原件） ----------

def insert_recheck(record: dict) -> bool:
    init_m7()
    with connect() as db:
        try:
            db.execute(
                """INSERT INTO m7_rechecks
                   (recheck_id, seal_id, issue_edition, baseline_plan_version,
                    target_plan_version, target_plan_digest, scope_digest,
                    check_result, diff, conclusion, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["recheck_id"],
                    record["seal_id"],
                    record["issue_edition"],
                    record["baseline_plan_version"],
                    record["target_plan_version"],
                    record["target_plan_digest"],
                    record["scope_digest"],
                    json.dumps(record["check_result"], ensure_ascii=False),
                    json.dumps(record["diff"], ensure_ascii=False),
                    record["conclusion"],
                    record["created_at"],
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_recheck(recheck_id_value: str) -> dict | None:
    init_m7()
    with connect() as db:
        row = db.execute(
            "SELECT * FROM m7_rechecks WHERE recheck_id = ?", (recheck_id_value,)
        ).fetchone()
    if not row:
        return None
    cols = ("recheck_id", "seal_id", "issue_edition", "baseline_plan_version",
            "target_plan_version", "target_plan_digest", "scope_digest",
            "check_result", "diff", "conclusion", "created_at")
    result = dict(zip(cols, row))
    result["check_result"] = json.loads(result["check_result"])
    result["diff"] = json.loads(result["diff"])
    return result


def list_rechecks(seal_id_value: str | None = None) -> list[dict]:
    init_m7()
    with connect() as db:
        if seal_id_value:
            rows = db.execute(
                "SELECT recheck_id, seal_id, issue_edition, baseline_plan_version, "
                "target_plan_version, conclusion, created_at FROM m7_rechecks "
                "WHERE seal_id = ? ORDER BY created_at",
                (seal_id_value,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT recheck_id, seal_id, issue_edition, baseline_plan_version, "
                "target_plan_version, conclusion, created_at FROM m7_rechecks ORDER BY created_at"
            ).fetchall()
    return [
        dict(zip(("recheck_id", "seal_id", "issue_edition", "baseline_plan_version",
                  "target_plan_version", "conclusion", "created_at"), r))
        for r in rows
    ]


# ---------- 复验断点续跑 ----------

def upsert_job(job: dict) -> None:
    init_m7()
    with connect() as db:
        db.execute(
            """INSERT INTO m7_recheck_jobs (job_id, target_plan_version, status, total,
               processed, created_at, updated_at, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,
               processed=excluded.processed, updated_at=excluded.updated_at, detail=excluded.detail""",
            (
                job["job_id"],
                job["target_plan_version"],
                job["status"],
                job["total"],
                job["processed"],
                job["created_at"],
                job["updated_at"],
                json.dumps(job["detail"], ensure_ascii=False),
            ),
        )


def get_job(job_id: str) -> dict | None:
    init_m7()
    with connect() as db:
        row = db.execute("SELECT * FROM m7_recheck_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return None
    cols = ("job_id", "target_plan_version", "status", "total", "processed",
            "created_at", "updated_at", "detail")
    result = dict(zip(cols, row))
    result["detail"] = json.loads(result["detail"])
    return result


def raise_conflict(code: str, message: str) -> None:
    raise DomainError(code, message, status=409)
