"""运输测温异常项处置意见（运输质控）。

规则（与 README 一致）：
  * 异常项来自可信测温复核：超温点（OOR-读数号）与缺测区间（GAP-前读数号-后读数号），
    标识稳定；证据摘要对异常项完整证据内容（时间范围、相关读数及校准证明、依当前有效
    保管链重算的归属、限值等）取 SHA-256，证据任何变化都会改变摘要；
  * 提交处置须携带全局唯一处置单号、异常项标识、所见摘要、处置人、认可（accepted）或
    排除（excluded）异常的意见及理由；仅接受当前仍存在且摘要一致的异常项——过期
    （摘要已变化）或消失（项已不存在）的项整单拒绝且不留记录；
  * 同一异常项在同一证据摘要下只接受一次处置；同处置单号同内容重试返回原结果（200），
    异内容冲突（409 disposition_conflict）；
  * 读数补录、证明补录或作废、保管链纠错使异常项或归属变化后，旧意见永久可查但标为
    失效（superseded），新摘要可重新处置；查询区分未处置、有效处置和失效历史；
  * 处置只新增处置记录：不改原始读数、既有可信复核、保管链版本或已封存案卷。
"""
import json
import sqlite3
from typing import Any

from .calibrations import get_trusted_temperature_review
from .db import utcnow_iso
from .schemas import TempDispositionIn
from .service import DomainError, _fingerprint, get_sample_or_404


# --------------------------------------------------------------------------- 异常项构建

def _reading_ref(match: dict[str, Any]) -> dict[str, Any]:
    """异常项相关读数（含所匹配的校准证明号）。"""
    return {
        "reading_id": match["reading_id"],
        "device_id": match["device_id"],
        "occurred_at": match["occurred_at"],
        "value_celsius": match["value_celsius"],
        "certificate_id": match["certificate_id"],
    }


def _anomaly_items(trusted: dict[str, Any]) -> list[dict[str, Any]]:
    """把可信测温复核结果整理为异常项清单（每项含稳定标识与证据摘要）。

    超温点：OOR-{reading_id}；缺测区间：GAP-{before_reading_id}-{after_reading_id}。
    证据摘要覆盖时间范围、相关读数及校准证明、归属（含在途移交事件号/待核明细）与限值，
    因此读数补录、证明补录/作废、保管链纠错都会使摘要变化；缺测端点变化则使旧项消失、
    新项出现。清单按时间范围起始与标识排序，输出稳定。
    """
    matches = {m["reading_id"]: m for m in trusted["reading_matches"]}
    items: list[dict[str, Any]] = []
    for p in trusted["out_of_range_points"]:
        content = {
            "item_id": f"OOR-{p['reading_id']}",
            "kind": "out_of_range",
            "time_range": {"start": p["occurred_at"], "end": p["occurred_at"]},
            "direction": p["direction"],
            "limit_celsius": p["limit_celsius"],
            "readings": [_reading_ref(matches[p["reading_id"]])],
            "attribution": p["attribution"],
        }
        items.append({"digest": _fingerprint(content), **content})
    for g in trusted["gap_intervals"]:
        content = {
            "item_id": f"GAP-{g['before_reading_id']}-{g['after_reading_id']}",
            "kind": "gap",
            "time_range": {"start": g["start"], "end": g["end"]},
            "duration_seconds": g["duration_seconds"],
            "max_interval_seconds": g["max_interval_seconds"],
            "readings": [
                _reading_ref(matches[g["before_reading_id"]]),
                _reading_ref(matches[g["after_reading_id"]]),
            ],
            "segments": g["segments"],
        }
        items.append({"digest": _fingerprint(content), **content})
    items.sort(key=lambda it: (it["time_range"]["start"], it["item_id"]))
    return items


def _current_anomalies(conn: sqlite3.Connection, sample_code: str) -> dict[str, dict[str, Any]]:
    """当前异常项：item_id → 异常项（含证据摘要）。样本/测温配置不存在时抛 404。"""
    trusted = get_trusted_temperature_review(conn, sample_code)
    return {it["item_id"]: it for it in _anomaly_items(trusted)}


# --------------------------------------------------------------------------- 视图

def _disposition_status(row: sqlite3.Row, current: dict[str, dict[str, Any]]) -> str:
    """处置意见现时效性：异常项当前仍存在且摘要一致为有效（effective），否则失效。"""
    item = current.get(row["item_id"])
    if item is not None and item["digest"] == row["item_digest"]:
        return "effective"
    return "superseded"


def _disposition_view(row: sqlite3.Row, status: str) -> dict[str, Any]:
    return {
        "disposition_no": row["disposition_no"],
        "sample_code": row["sample_code"],
        "item_id": row["item_id"],
        "item_digest": row["item_digest"],
        "operator": row["operator"],
        "verdict": row["verdict"],
        "reason": row["reason"],
        "status": status,
        "created_at": row["created_at"],
        "item_snapshot": json.loads(row["item_snapshot_json"]),
    }


def _get_by_no(conn: sqlite3.Connection, disposition_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM temp_disposition WHERE disposition_no = ?", (disposition_no,)
    ).fetchone()


def _get_by_item_digest(
    conn: sqlite3.Connection, sample_id: int, item_id: str, item_digest: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM temp_disposition WHERE sample_id = ? AND item_id = ? AND item_digest = ?",
        (sample_id, item_id, item_digest),
    ).fetchone()


def _request_fingerprint(sample_code: str, data: TempDispositionIn) -> str:
    return _fingerprint({
        "disposition_no": data.disposition_no,
        "sample_code": sample_code,
        "item_id": data.item_id,
        "observed_digest": data.observed_digest,
        "operator": data.operator,
        "verdict": data.verdict,
        "reason": data.reason,
    })


# --------------------------------------------------------------------------- 提交处置

def submit_disposition(
    conn: sqlite3.Connection, sample_code: str, data: TempDispositionIn
) -> tuple[dict[str, Any], bool]:
    """提交异常项处置意见。返回 (处置视图, 是否同单号同内容幂等重试)。

    仅接受当前仍存在且摘要一致的异常项；同一异常项同一摘要只接受一次处置；
    同处置单号同内容重试返回原结果（即使该项此后已失效），异内容冲突。
    任何拒绝均不留记录。
    """
    sample = get_sample_or_404(conn, sample_code)
    h = _request_fingerprint(sample_code, data)

    # 幂等查重优先：同单号同内容重试返回原结果（项事后失效也成立）
    existing = _get_by_no(conn, data.disposition_no)
    if existing is not None:
        if existing["request_hash"] != h:
            raise DomainError(
                409,
                "disposition_conflict",
                f"处置单号 {data.disposition_no} 已存在但提交内容不一致（同单号异内容冲突）",
                {"disposition_no": data.disposition_no,
                 "existing_request_hash": existing["request_hash"],
                 "incoming_request_hash": h},
            )
        current = _current_anomalies(conn, sample_code)
        return _disposition_view(existing, _disposition_status(existing, current)), True

    # 当前异常项（样本无测温配置 → 404 temp_config_not_found）
    current = _current_anomalies(conn, sample_code)
    item = current.get(data.item_id)
    if item is None:
        raise DomainError(
            409,
            "disposition_item_stale",
            f"异常项 {data.item_id} 在当前可信测温复核中已不存在"
            "（证据变化导致项消失，或标识从未存在），整单拒绝且不留记录",
            {"item_id": data.item_id, "current_item_ids": sorted(current)},
        )
    if item["digest"] != data.observed_digest:
        raise DomainError(
            409,
            "disposition_digest_mismatch",
            f"异常项 {data.item_id} 的证据摘要已变化"
            "（读数补录、证明补录或作废、保管链纠错所致），所见摘要过期，整单拒绝且不留记录",
            {"item_id": data.item_id,
             "observed_digest": data.observed_digest,
             "current_digest": item["digest"]},
        )
    dup = _get_by_item_digest(conn, sample["id"], data.item_id, item["digest"])
    if dup is not None:
        raise DomainError(
            409,
            "disposition_duplicate",
            f"异常项 {data.item_id} 在同一证据摘要下已被处置单 {dup['disposition_no']} 处置，"
            "同一异常项在同一证据摘要下只接受一次处置",
            {"item_id": data.item_id, "item_digest": item["digest"],
             "existing_disposition_no": dup["disposition_no"]},
        )

    try:
        cur = conn.execute(
            "INSERT INTO temp_disposition"
            " (disposition_no, sample_id, sample_code, item_id, item_digest,"
            "  operator, verdict, reason, item_snapshot_json, request_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (data.disposition_no, sample["id"], sample["sample_code"], data.item_id,
             item["digest"], data.operator, data.verdict, data.reason,
             json.dumps(item, ensure_ascii=False, sort_keys=True), h, utcnow_iso()),
        )
    except sqlite3.IntegrityError:
        # 并发下唯一键竞争（处置单号或 样本+项+摘要 被抢先占用）：按已提交结果判定
        raced = _get_by_no(conn, data.disposition_no)
        if raced is not None:
            if raced["request_hash"] == h:
                return _disposition_view(raced, _disposition_status(raced, current)), True
            raise DomainError(
                409, "disposition_conflict",
                f"处置单号 {data.disposition_no} 并发写入且内容冲突",
            )
        raced_dup = _get_by_item_digest(conn, sample["id"], data.item_id, item["digest"])
        if raced_dup is not None:
            raise DomainError(
                409, "disposition_duplicate",
                f"异常项 {data.item_id} 在同一证据摘要下已被并发处置单 "
                f"{raced_dup['disposition_no']} 抢先处置",
                {"item_id": data.item_id, "item_digest": item["digest"],
                 "existing_disposition_no": raced_dup["disposition_no"]},
            )
        raise

    row = conn.execute("SELECT * FROM temp_disposition WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _disposition_view(row, "effective"), False


# --------------------------------------------------------------------------- 处置查询

def get_disposition_review(conn: sqlite3.Connection, sample_code: str) -> dict[str, Any]:
    """按样本查询当前异常项及处置状态（未处置/有效处置）与失效历史。

    每项返回稳定标识、证据摘要、时间范围、相关读数及校准证明、依当前有效保管链
    重算的归属（待核明确标记为 pending_review，不臆定责任人）；失效历史永久可查，
    附处置时的完整证据快照。
    """
    sample = get_sample_or_404(conn, sample_code)
    trusted = get_trusted_temperature_review(conn, sample_code)
    items = _anomaly_items(trusted)
    current_keys = {(it["item_id"], it["digest"]) for it in items}

    rows = conn.execute(
        "SELECT * FROM temp_disposition WHERE sample_id = ? ORDER BY id ASC",
        (sample["id"],),
    ).fetchall()
    valid = {(r["item_id"], r["item_digest"]): r for r in rows}

    out_items: list[dict[str, Any]] = []
    disposed_count = 0
    for item in items:
        row = valid.get((item["item_id"], item["digest"]))
        if row is not None:
            disposed_count += 1
        out_items.append({
            **item,
            "disposition_status": "disposed" if row is not None else "undisposed",
            "disposition": _disposition_view(row, "effective") if row is not None else None,
        })

    history = [
        _disposition_view(r, "superseded")
        for r in rows
        if (r["item_id"], r["item_digest"]) not in current_keys
    ]

    return {
        "sample_code": sample["sample_code"],
        "assessment_status": trusted["assessment_status"],
        "item_count": len(out_items),
        "undisposed_count": len(out_items) - disposed_count,
        "disposed_count": disposed_count,
        "superseded_count": len(history),
        "items": out_items,
        "history": history,
    }
