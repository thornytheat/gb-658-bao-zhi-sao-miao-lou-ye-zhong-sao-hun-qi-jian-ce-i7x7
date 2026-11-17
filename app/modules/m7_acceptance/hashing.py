"""确定性摘要与标识：计划、扫描范围、签章、复验均可复算、可回读。"""
from hashlib import sha256


def digest(payload: str | bytes) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return sha256(payload).hexdigest()


def short_digest(payload: str | bytes) -> str:
    return digest(payload)[:12]


def canonical_json(data) -> str:
    """键排序、无空白的稳定 JSON 文本，作为摘要输入。"""
    import json

    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def plan_digest(issue_edition: str, version: int, expected_pages: list[str], basis: str) -> str:
    return digest(
        canonical_json(
            {
                "issue_edition": issue_edition,
                "version": version,
                "expected_pages": expected_pages,
                "basis": basis,
            }
        )
    )


def scope_digest(issue_edition: str, rows: list[dict]) -> str:
    """扫描范围摘要只取采纳行的稳定字段，重扫/隔离行不进入签章范围。"""
    compact = [
        {
            "page": row["page"],
            "file_name": row["file_name"],
            "sha256": row["sha256"],
            "version": int(row.get("version", 1)),
            "clarity": int(row.get("clarity", 0)),
        }
        for row in sorted(rows, key=lambda r: (int(r["page"]), r["file_name"]))
    ]
    return digest(canonical_json({"issue_edition": issue_edition, "rows": compact}))


def seal_id(issue_edition: str, plan_version: int, plan_digest_value: str, scope_digest_value: str) -> str:
    raw = "|".join((issue_edition, str(plan_version), plan_digest_value, scope_digest_value))
    return "SEAL-" + short_digest(raw)


def recheck_id(seal_id_value: str, plan_version: int) -> str:
    return "CHK-" + short_digest("|".join((seal_id_value, str(plan_version))))
