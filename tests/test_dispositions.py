"""端到端自测：运输测温异常项处置意见（运输质控）。

运行：python -m tests.test_dispositions
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_dispositions.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()
API = "/api/v1"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def reg(code, collector="王采集", seal="SEAL-D", when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when, "collector": collector, "seal_no": seal})


def evt(eid, etype, code, when, **kw):
    return client.post(f"{API}/events", json={
        "event_id": eid, "event_type": etype, "sample_code": code, "occurred_at": when, **kw})


def cfg(code, lo=2.0, hi=8.0, interval=1800):
    return client.post(f"{API}/temperature-configs", json={
        "sample_code": code, "min_celsius": lo, "max_celsius": hi,
        "max_interval_seconds": interval})


def rd(code, rid, when, value, dev):
    return client.post(f"{API}/temperature-readings", json={
        "sample_code": code, "reading_id": rid, "device_id": dev,
        "occurred_at": when, "value_celsius": value})


def cert(cid, dev, start, end, issuer="计量员甲"):
    return client.post(f"{API}/temperature-calibrations", json={
        "certificate_id": cid, "device_id": dev, "valid_from": start,
        "valid_to": end, "issued_by": issuer})


def void_cert(cid, reason="证明文件复核作废"):
    return client.post(f"{API}/temperature-calibrations/{cid}/void", json={"reason": reason})


def correct(cid, code, target, action="void", reason="纠错"):
    return client.post(f"{API}/corrections", json={
        "correction_id": cid, "sample_code": code, "target_event_id": target,
        "action": action, "operator": "质控员", "reason": reason})


def disp(code, no, item_id, digest, verdict="accepted", operator="质控员甲", reason="处置理由"):
    return client.post(f"{API}/samples/{code}/temperature/dispositions", json={
        "disposition_no": no, "item_id": item_id, "observed_digest": digest,
        "operator": operator, "verdict": verdict, "reason": reason})


def disps(code):
    return client.get(f"{API}/samples/{code}/temperature/dispositions")


def item_of(view, item_id):
    return next((x for x in view["items"] if x["item_id"] == item_id), None)


def versions(code):
    return client.get(f"{API}/samples/{code}/versions").json()


# ---------------------------------------------------------------- 1. 当前异常项查询：稳定标识/摘要/时间范围/读数证明/归属
print("1) 处置查询：当前异常项的稳定标识、证据摘要、时间范围、相关读数及校准证明、归属")
reg("DSP-1")
evt("D1-EVT-T1", "transfer", "DSP-1", "2026-09-30T02:00:00Z",
    transfer_no="D1-TR", from_custodian="王采集", to_custodian="李运输")
evt("D1-EVT-R1", "receive", "DSP-1", "2026-09-30T04:00:00Z",
    transfer_no="D1-TR", receiver="李运输", seal_no="SEAL-D")
cfg("DSP-1")
rd("DSP-1", "D1-RD-1", "2026-09-30T01:30:00Z", 4.0, "DEV-D1")
rd("DSP-1", "D1-RD-2", "2026-09-30T02:30:00Z", 9.5, "DEV-D1")   # 超温（在途）
rd("DSP-1", "D1-RD-3", "2026-09-30T05:00:00Z", 5.0, "DEV-D1")   # 与 RD-2 形成跨签收缺测
cert("D1-CAL-1", "DEV-D1", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z")

r = disps("DSP-1")
check("处置查询 200", r.status_code == 200, r.text)
v = r.json()
check("异常项计数与初始状态",
      v["item_count"] == 3 and v["undisposed_count"] == 3
      and v["disposed_count"] == 0 and v["superseded_count"] == 0 and v["history"] == [], str(v))
ids = [x["item_id"] for x in v["items"]]
check("稳定标识：超温 OOR-读数号、缺测 GAP-前-后读数号，按时间排序",
      ids == ["GAP-D1-RD-1-D1-RD-2", "GAP-D1-RD-2-D1-RD-3", "OOR-D1-RD-2"], str(ids))
oor = item_of(v, "OOR-D1-RD-2")
check("超温项：时间范围=读数时刻，相关读数带校准证明号",
      oor["kind"] == "out_of_range"
      and oor["time_range"] == {"start": "2026-09-30T02:30:00Z", "end": "2026-09-30T02:30:00Z"}
      and oor["direction"] == "above_max" and oor["limit_celsius"] == 8.0
      and oor["readings"] == [{"reading_id": "D1-RD-2", "device_id": "DEV-D1",
                               "occurred_at": "2026-09-30T02:30:00Z", "value_celsius": 9.5,
                               "certificate_id": "D1-CAL-1"}], str(oor))
check("超温项归属依当前有效链重算（在途移交）",
      oor["attribution"]["kind"] == "in_transit"
      and oor["attribution"]["transfer_no"] == "D1-TR"
      and oor["attribution"]["transfer_event_id"] == "D1-EVT-T1", str(oor["attribution"]))
check("每项带 64 位证据摘要且互不相同的当前项摘要",
      all(len(x["digest"]) == 64 for x in v["items"])
      and len({x["digest"] for x in v["items"]}) == 3, str([x["digest"] for x in v["items"]]))
gap = item_of(v, "GAP-D1-RD-2-D1-RD-3")
check("缺测项：时间范围/限值/两侧读数及证明/跨签收拆分归属",
      gap["kind"] == "gap" and gap["duration_seconds"] == 9000
      and gap["max_interval_seconds"] == 1800
      and [x["reading_id"] for x in gap["readings"]] == ["D1-RD-2", "D1-RD-3"]
      and all(x["certificate_id"] == "D1-CAL-1" for x in gap["readings"])
      and [s["attribution"]["kind"] for s in gap["segments"]] == ["in_transit", "custodian"]
      and gap["segments"][1]["attribution"]["custodian"] == "李运输", str(gap))
check("初始均未处置", all(x["disposition_status"] == "undisposed" and x["disposition"] is None
                          for x in v["items"]), str(v["items"]))

# ---------------------------------------------------------------- 2. 提交处置：认可/排除、幂等、冲突、同摘要仅一次
print("2) 提交处置：认可/排除，同单号幂等/异内容冲突，同项同摘要仅一次")
r = disp("DSP-1", "DSP-NO-1", "OOR-D1-RD-2", oor["digest"], verdict="accepted",
         operator="质控员甲", reason="运输途中制冷机组故障，认可超温异常")
check("提交处置 201 且有效", r.status_code == 201 and r.json()["idempotent"] is False
      and r.json()["disposition"]["status"] == "effective", r.text)
d1 = r.json()["disposition"]
check("处置记录字段（单号/项/摘要/处置人/意见/理由/快照）",
      d1["disposition_no"] == "DSP-NO-1" and d1["item_id"] == "OOR-D1-RD-2"
      and d1["item_digest"] == oor["digest"] and d1["operator"] == "质控员甲"
      and d1["verdict"] == "accepted" and d1["reason"].startswith("运输途中")
      and d1["sample_code"] == "DSP-1"
      and d1["item_snapshot"]["item_id"] == "OOR-D1-RD-2"
      and d1["item_snapshot"]["digest"] == oor["digest"], str(d1))
r = disp("DSP-1", "DSP-NO-1", "OOR-D1-RD-2", oor["digest"], verdict="accepted",
         operator="质控员甲", reason="运输途中制冷机组故障，认可超温异常")
check("同单号同内容重试 200 返回原结果",
      r.status_code == 200 and r.json()["idempotent"] is True
      and r.json()["disposition"]["created_at"] == d1["created_at"]
      and r.json()["disposition"]["status"] == "effective", r.text)
r = disp("DSP-1", "DSP-NO-1", "OOR-D1-RD-2", oor["digest"], verdict="excluded", reason="改内容")
check("同单号异内容 409 disposition_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "disposition_conflict", r.text)
r = disp("DSP-1", "DSP-NO-2", "OOR-D1-RD-2", oor["digest"], verdict="excluded", reason="换人再处置")
check("同项同摘要再次处置 409 disposition_duplicate",
      r.status_code == 409 and r.json()["error"]["code"] == "disposition_duplicate"
      and r.json()["error"]["details"]["existing_disposition_no"] == "DSP-NO-1", r.text)
r = disp("DSP-1", "DSP-NO-3", "GAP-D1-RD-2-D1-RD-3", gap["digest"], verdict="excluded",
         operator="质控员乙", reason="期间样本处于备用保温箱，排除缺测异常")
check("排除异常意见 201", r.status_code == 201
      and r.json()["disposition"]["verdict"] == "excluded", r.text)
v = disps("DSP-1").json()
check("查询区分未处置与有效处置",
      v["disposed_count"] == 2 and v["undisposed_count"] == 1
      and item_of(v, "OOR-D1-RD-2")["disposition_status"] == "disposed"
      and item_of(v, "OOR-D1-RD-2")["disposition"]["disposition_no"] == "DSP-NO-1"
      and item_of(v, "GAP-D1-RD-1-D1-RD-2")["disposition_status"] == "undisposed"
      and item_of(v, "GAP-D1-RD-1-D1-RD-2")["disposition"] is None, str(v["counts"] if "counts" in v else v["items"]))

# ---------------------------------------------------------------- 3. 过期/消失整单拒绝且不留记录
print("3) 过期或消失的异常项整单拒绝且不留记录")
r = disp("DSP-1", "DSP-NO-4", "GAP-D1-RD-1-D1-RD-2", "0" * 64, reason="错误摘要")
check("摘要过期 409 disposition_digest_mismatch（详情给当前摘要）",
      r.status_code == 409 and r.json()["error"]["code"] == "disposition_digest_mismatch"
      and r.json()["error"]["details"]["current_digest"]
      == item_of(disps("DSP-1").json(), "GAP-D1-RD-1-D1-RD-2")["digest"], r.text)
r = disp("DSP-1", "DSP-NO-5", "OOR-NO-SUCH", "0" * 64, reason="不存在的项")
check("异常项消失/不存在 409 disposition_item_stale",
      r.status_code == 409 and r.json()["error"]["code"] == "disposition_item_stale"
      and "OOR-D1-RD-2" in r.json()["error"]["details"]["current_item_ids"], r.text)
v = disps("DSP-1").json()
check("拒绝不留记录（无新处置、无失效历史、单号可复用）",
      v["disposed_count"] == 2 and v["superseded_count"] == 0, str(v))
r = disp("DSP-1", "DSP-NO-4", "GAP-D1-RD-1-D1-RD-2",
         item_of(v, "GAP-D1-RD-1-D1-RD-2")["digest"], reason="修正摘要后复用失败单号")
check("失败单号修正后可复用 201", r.status_code == 201, r.text)

# ---------------------------------------------------------------- 4. 证明作废/补录使项变化：旧意见失效，新摘要可重新处置
print("4) 证明作废/补录：旧意见标失效永久可查，新摘要可重新处置")
before_versions = versions("DSP-1")["current_version"]
before_trusted = client.get(f"{API}/samples/DSP-1/temperature/trusted").json()
r = void_cert("D1-CAL-1", "证明晚到复核不通过")
check("作废证明 201", r.status_code == 201, r.text)
v = disps("DSP-1").json()
check("作废后异常项消失（无法评估），旧意见全部失效",
      v["assessment_status"] == "unable_to_assess" and v["item_count"] == 0
      and v["superseded_count"] == 3
      and {h["disposition_no"] for h in v["history"]} == {"DSP-NO-1", "DSP-NO-3", "DSP-NO-4"}
      and all(h["status"] == "superseded" for h in v["history"]), str(v))
check("失效历史保留处置时证据快照与理由",
      all(h["item_snapshot"]["item_id"] == h["item_id"] and h["reason"] for h in v["history"]),
      str(v["history"]))
r = disp("DSP-1", "DSP-NO-6", "OOR-D1-RD-2", "0" * 64, reason="项已消失")
check("项消失后提交 409 disposition_item_stale 且不留记录",
      r.status_code == 409 and r.json()["error"]["code"] == "disposition_item_stale"
      and disps("DSP-1").json()["superseded_count"] == 3, r.text)
r = disp("DSP-1", "DSP-NO-1", "OOR-D1-RD-2", oor["digest"], verdict="accepted",
         operator="质控员甲", reason="运输途中制冷机组故障，认可超温异常")
check("失效后同单号同内容重试仍 200 返回原结果（标失效）",
      r.status_code == 200 and r.json()["idempotent"] is True
      and r.json()["disposition"]["status"] == "superseded", r.text)

cert("D1-CAL-2", "DEV-D1", "2026-09-30T01:00:00Z", "2026-09-30T06:00:00Z", issuer="计量员乙")
v = disps("DSP-1").json()
oor_new = item_of(v, "OOR-D1-RD-2")
check("补录新证明后异常项重现但摘要变化（证明号是证据一部分）",
      v["item_count"] == 3 and oor_new is not None
      and oor_new["digest"] != oor["digest"]
      and oor_new["readings"][0]["certificate_id"] == "D1-CAL-2"
      and oor_new["disposition_status"] == "undisposed", str(oor_new))
check("旧意见仍失效可查", v["superseded_count"] == 3, str(v))
r = disp("DSP-1", "DSP-NO-7", "OOR-D1-RD-2", oor_new["digest"], reason="新证明下重新认可超温")
check("新摘要可重新处置 201", r.status_code == 201
      and r.json()["disposition"]["status"] == "effective", r.text)
v = disps("DSP-1").json()
check("重新处置后：新意见有效、旧意见仍失效",
      item_of(v, "OOR-D1-RD-2")["disposition"]["disposition_no"] == "DSP-NO-7"
      and v["disposed_count"] == 1 and v["superseded_count"] == 3, str(v))
check("处置不产生保管链版本、不改既有可信复核",
      versions("DSP-1")["current_version"] == before_versions
      and client.get(f"{API}/samples/DSP-1/temperature/trusted").json()["reading_matches"]
      == client.get(f"{API}/samples/DSP-1/temperature/trusted").json()["reading_matches"]
      and len(client.get(f"{API}/samples/DSP-1/temperature/trusted").json()["reading_matches"])
      == len(before_trusted["reading_matches"]), str(before_versions))

# ---------------------------------------------------------------- 5. 保管链纠错改变归属 → 摘要变化、旧意见失效
print("5) 保管链纠错改变归属：受影响项摘要变化，旧意见失效，未受影响项不变")
v = disps("DSP-1").json()
gap23 = item_of(v, "GAP-D1-RD-2-D1-RD-3")
r = disp("DSP-1", "DSP-NO-8", "GAP-D1-RD-2-D1-RD-3", gap23["digest"], reason="认可跨签收缺测")
check("先处置跨签收缺测项 201", r.status_code == 201, r.text)
r = correct("D1-FIX-1", "DSP-1", "D1-EVT-R1", reason="签收记录错误作废")
check("作废签收纠错 201", r.status_code == 201, r.text)
v = disps("DSP-1").json()
gap23_new = item_of(v, "GAP-D1-RD-2-D1-RD-3")
check("纠错后缺测项归属重算（签收后段回到在途）且摘要变化",
      gap23_new is not None and gap23_new["digest"] != gap23["digest"]
      and all(s["attribution"]["kind"] == "in_transit" for s in gap23_new["segments"]),
      str(gap23_new))
check("受影响处置标失效、未受影响处置仍有效",
      item_of(v, "OOR-D1-RD-2")["disposition_status"] == "disposed"
      and item_of(v, "OOR-D1-RD-2")["disposition"]["disposition_no"] == "DSP-NO-7"
      and gap23_new["disposition_status"] == "undisposed"
      and any(h["disposition_no"] == "DSP-NO-8" and h["status"] == "superseded"
              for h in v["history"]), str(v))
r = disp("DSP-1", "DSP-NO-9", "GAP-D1-RD-2-D1-RD-3", gap23["digest"], reason="用旧摘要")
check("纠错后用旧摘要提交 409 disposition_digest_mismatch",
      r.status_code == 409 and r.json()["error"]["code"] == "disposition_digest_mismatch", r.text)

# ---------------------------------------------------------------- 6. 读数补录使缺测项消失/拆分
print("6) 读数补录：缺测项消失或拆分后旧意见失效")
reg("DSP-2")
cfg("DSP-2")
rd("DSP-2", "D2-RD-1", "2026-09-30T01:00:00Z", 4.0, "DEV-D2")
rd("DSP-2", "D2-RD-3", "2026-09-30T03:00:00Z", 5.0, "DEV-D2")
cert("D2-CAL-1", "DEV-D2", "2026-09-30T00:00:00Z", "2026-09-30T06:00:00Z")
v = disps("DSP-2").json()
check("补录前单一缺测项", [x["item_id"] for x in v["items"]] == ["GAP-D2-RD-1-D2-RD-3"], str(v))
r = disp("DSP-2", "DSP-NO-10", "GAP-D2-RD-1-D2-RD-3", v["items"][0]["digest"], reason="认可缺测")
check("处置缺测项 201", r.status_code == 201, r.text)
rd("DSP-2", "D2-RD-2", "2026-09-30T02:00:00Z", 4.5, "DEV-D2")   # 乱序补录中间读数
v = disps("DSP-2").json()
ids = [x["item_id"] for x in v["items"]]
check("补录后原缺测项消失、拆分为两个新项",
      ids == ["GAP-D2-RD-1-D2-RD-2", "GAP-D2-RD-2-D2-RD-3"], str(ids))
check("原处置标失效（项消失）",
      v["superseded_count"] == 1 and v["history"][0]["disposition_no"] == "DSP-NO-10"
      and v["history"][0]["status"] == "superseded", str(v["history"]))
r = disp("DSP-2", "DSP-NO-11", "GAP-D2-RD-1-D2-RD-3", "0" * 64, reason="已消失的项")
check("消失的缺测项提交 409 disposition_item_stale",
      r.status_code == 409 and r.json()["error"]["code"] == "disposition_item_stale", r.text)

# ---------------------------------------------------------------- 7. 断链待核归属明确标记，不臆定责任人
print("7) 归属待核：明确标记 pending_review，不臆定责任人")
reg("DSP-3")
evt("D3-EVT-T1", "transfer", "DSP-3", "2026-09-30T02:00:00Z",
    transfer_no="D3-TR", from_custodian="张无关", to_custodian="李运输")  # 非当前保管人 → 断链
cfg("DSP-3")
rd("DSP-3", "D3-RD-1", "2026-09-30T03:00:00Z", 9.9, "DEV-D3")
cert("D3-CAL-1", "DEV-D3", "2026-09-30T00:00:00Z", "2026-09-30T06:00:00Z")
v = disps("DSP-3").json()
oor3 = item_of(v, "OOR-D3-RD-1")
check("断链后超温项归属标待核且无责任人字段",
      oor3["attribution"]["kind"] == "pending_review"
      and "custodian" not in oor3["attribution"]
      and "detail" in oor3["attribution"], str(oor3["attribution"]))
r = disp("DSP-3", "DSP-NO-12", "OOR-D3-RD-1", oor3["digest"], reason="归属待核，仅先认可异常事实")
check("待核项仍可提交处置 201", r.status_code == 201, r.text)

# ---------------------------------------------------------------- 8. 查询/提交错误与字段校验
print("8) 错误与字段校验")
r = disps("NO-SUCH-SAMPLE")
check("未知样本查询 404 sample_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "sample_not_found", r.text)
r = disp("NO-SUCH-SAMPLE", "DSP-NO-X", "OOR-X", "0" * 64)
check("未知样本提交 404 sample_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "sample_not_found", r.text)
reg("DSP-NOCFG")
r = disps("DSP-NOCFG")
check("未设测温配置查询 404 temp_config_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "temp_config_not_found", r.text)
r = disp("DSP-NOCFG", "DSP-NO-Y", "OOR-X", "0" * 64)
check("未设测温配置提交 404 temp_config_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "temp_config_not_found", r.text)
r = client.post(f"{API}/samples/DSP-1/temperature/dispositions", json={
    "disposition_no": "DSP-NO-B1", "item_id": "OOR-D1-RD-2", "observed_digest": "0" * 64,
    "operator": "质控员", "verdict": "accepted", "reason": "   "})
check("空白理由 422", r.status_code == 422, r.text)
r = client.post(f"{API}/samples/DSP-1/temperature/dispositions", json={
    "disposition_no": "DSP-NO-B2", "item_id": "OOR-D1-RD-2", "observed_digest": "0" * 64,
    "operator": "  ", "verdict": "accepted", "reason": "理由"})
check("空白处置人 422", r.status_code == 422, r.text)
r = client.post(f"{API}/samples/DSP-1/temperature/dispositions", json={
    "disposition_no": "DSP-NO-B3", "item_id": "OOR-D1-RD-2", "observed_digest": "0" * 64,
    "operator": "质控员", "verdict": "maybe", "reason": "理由"})
check("非法意见取值 422", r.status_code == 422, r.text)
r = client.post(f"{API}/samples/DSP-1/temperature/dispositions", json={
    "disposition_no": "DSP-NO-B4", "item_id": "OOR-D1-RD-2",
    "operator": "质控员", "verdict": "accepted", "reason": "理由"})
check("缺所见摘要 422", r.status_code == 422, r.text)
v = disps("DSP-1").json()
check("字段校验失败不留记录", v["superseded_count"] == 4
      and all(not h["disposition_no"].startswith("DSP-NO-B") for h in v["history"]), str(v))

# ---------------------------------------------------------------- 9. 核定后仍可处置（与证明补录/作废一致）
print("9) 核定入库后仍可查询与处置（只冻结证据写入，不冻结质控处置）")
reg("DSP-4")
evt("D4-EVT-T1", "transfer", "DSP-4", "2026-09-30T01:30:00Z",
    transfer_no="D4-TR", from_custodian="王采集", to_custodian="李运输")
evt("D4-EVT-R1", "receive", "DSP-4", "2026-09-30T02:00:00Z",
    transfer_no="D4-TR", receiver="李运输", seal_no="SEAL-D")
evt("D4-EVT-S1", "storage", "DSP-4", "2026-09-30T02:30:00Z", custodian="实验室")
cfg("DSP-4")
rd("DSP-4", "D4-RD-1", "2026-09-30T01:45:00Z", 9.0, "DEV-D4")
cert("D4-CAL-1", "DEV-D4", "2026-09-30T01:00:00Z", "2026-09-30T03:00:00Z")
client.post(f"{API}/samples/DSP-4/approve", json={"approver": "质量主管"})
v = disps("DSP-4").json()
check("核定后处置查询可用", v["item_count"] == 1
      and v["items"][0]["item_id"] == "OOR-D4-RD-1", str(v))
r = disp("DSP-4", "DSP-NO-13", "OOR-D4-RD-1", v["items"][0]["digest"], reason="核定后补充处置")
check("核定后仍可提交处置 201", r.status_code == 201, r.text)

# ---------------------------------------------------------------- 10. 不改原始证据、既有可信复核、保管链版本与封存案卷
print("10) 处置不改变原始证据、既有可信复核、保管链版本及封存案卷")
reg("DSP-5")
cfg("DSP-5")
rd("DSP-5", "D5-RD-1", "2026-09-30T02:00:00Z", 3.0, "DEV-D5")
cert("D5-CAL-1", "DEV-D5", "2026-09-30T00:00:00Z", "2026-09-30T06:00:00Z")
r = client.post(f"{API}/dossiers", json={"dossier_id": "DSP-DOSSIER-1", "items": [
    {"sample_code": "DSP-1"}, {"sample_code": "DSP-5"}]})
check("封存案卷 201", r.status_code == 201, r.text)
digest_before = r.json()["dossier"]["digest"]
ver_before = {c: versions(c)["current_version"] for c in ("DSP-1", "DSP-5")}
trusted_before = client.get(f"{API}/samples/DSP-5/temperature/trusted").json()
temp_before = client.get(f"{API}/samples/DSP-5/temperature").json()
v5 = disps("DSP-5").json()
check("DSP-5 当前无异常项（全部读数在限内）", v5["item_count"] == 0, str(v5))
# 在 DSP-1 上再做一次处置（新摘要项 GAP-D1-RD-1-D1-RD-2 已处置过，用 DSP-5 制造一个超温再处置）
rd("DSP-5", "D5-RD-2", "2026-09-30T03:00:00Z", 9.9, "DEV-D5")
v5 = disps("DSP-5").json()
r = disp("DSP-5", "DSP-NO-14", "OOR-D5-RD-2", item_of(v5, "OOR-D5-RD-2")["digest"],
         reason="认可超温，留待复测")
check("案卷封存后提交处置 201", r.status_code == 201, r.text)
check("案卷摘要不因处置改变",
      client.post(f"{API}/dossiers/DSP-DOSSIER-1/verify").json()["match"] is True
      and client.get(f"{API}/dossiers/DSP-DOSSIER-1").json()["dossier"]["digest"] == digest_before,
      str(digest_before))
check("保管链版本不因处置改变",
      all(versions(c)["current_version"] == n for c, n in ver_before.items()), str(ver_before))
trusted_after = client.get(f"{API}/samples/DSP-5/temperature/trusted").json()
temp_after = client.get(f"{API}/samples/DSP-5/temperature").json()
check("既有可信复核与原始测温查询不因处置改变（读数与结果一致）",
      trusted_after["reading_count"] == trusted_before["reading_count"] + 1  # 仅新增的那条读数
      and temp_after["reading_count"] == temp_before["reading_count"] + 1
      and [x["reading_id"] for x in trusted_after["out_of_range_points"]] == ["D5-RD-2"]
      and "disposition" not in trusted_after["reading_matches"][0], str(trusted_after))

# ---------------------------------------------------------------- 11. 并发
print("11) 并发：同单号同内容一建一幂等；同项同摘要不同单号一建一重复")
reg("DSP-6")
cfg("DSP-6")
rd("DSP-6", "D6-RD-1", "2026-09-30T02:00:00Z", 9.0, "DEV-D6")
cert("D6-CAL-1", "DEV-D6", "2026-09-30T00:00:00Z", "2026-09-30T06:00:00Z")
v6 = disps("DSP-6").json()
d6_digest = v6["items"][0]["digest"]

results: dict[str, int] = {}
barrier = threading.Barrier(2)


def post_same(tag):
    barrier.wait()
    results[tag] = disp("DSP-6", "DSP-NO-C1", "OOR-D6-RD-1", d6_digest,
                        reason="并发同内容").status_code


t1 = threading.Thread(target=post_same, args=("A",))
t2 = threading.Thread(target=post_same, args=("B",))
t1.start(); t2.start(); t1.join(); t2.join()
check("同单号同内容并发：一 201 一 200", sorted(results.values()) == [200, 201], str(results))

results.clear()
barrier = threading.Barrier(2)
# 另起一份样本的异常项，避免与上面已处置的项冲突
reg("DSP-7")
cfg("DSP-7")
rd("DSP-7", "D7-RD-1", "2026-09-30T02:00:00Z", 9.0, "DEV-D7")
cert("D7-CAL-1", "DEV-D7", "2026-09-30T00:00:00Z", "2026-09-30T06:00:00Z")
d7_digest = disps("DSP-7").json()["items"][0]["digest"]


def post_dup(tag, no):
    barrier.wait()
    results[tag] = disp("DSP-7", no, "OOR-D7-RD-1", d7_digest,
                        verdict="excluded", reason="并发抢处置").status_code


t1 = threading.Thread(target=post_dup, args=("C", "DSP-NO-C2"))
t2 = threading.Thread(target=post_dup, args=("D", "DSP-NO-C3"))
t1.start(); t2.start(); t1.join(); t2.join()
check("同项同摘要不同单号并发：一 201 一 409 disposition_duplicate",
      sorted(results.values()) == [201, 409], str(results))
v7 = disps("DSP-7").json()
check("并发后同项同摘要仅一条有效处置",
      v7["disposed_count"] == 1 and v7["superseded_count"] == 0, str(v7))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
