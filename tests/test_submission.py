"""Regression tests for finalization, evidence and monitoring defects found before submission.

All databases are in memory; the application demo database is never opened.
"""
import copy
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))
import seed
from engine import scenarios as sc, llm as L
from engine.pipeline import Pipeline

D = sc.DEMO_DATE


def fresh():
    con = sqlite3.connect(":memory:")
    with tempfile.TemporaryDirectory() as tmp:
        db = seed.build(Path(tmp) / "seed.db", date.fromisoformat(D))
        source = sqlite3.connect(db)
        source.backup(con)
        source.close()
    return Pipeline(con)


def blocked(fn, exc=ValueError):
    try:
        fn()
    except exc:
        return
    raise AssertionError("Unsafe operation was not blocked")


def s1(p):
    r = copy.deepcopy(sc.S1["request"])
    j, _ = p.review(r, sc.S1["planning"], sc.S1["now"])
    return r, j


def hot(p, confined=False):
    r = copy.deepcopy(sc.S3["request"])
    r.update(zone_id="W1", equipment_id=None, requester="W005", workers=["W005"],
             claimed_no_combustibles=False, fire_watch="W001")
    plan = copy.deepcopy(sc.S3["planning"])
    if confined:
        plan["1"] = {"answer": "해당", "reason": "현장 확인"}
    p.con.execute("INSERT INTO gas_readings(zone_id,ts,lel_percent,o2,co2,co_ppm,h2s_ppm) "
                  "VALUES('W1',?,2,20.9,0.04,0,0)", (f"{D}T13:40",))
    j, _ = p.review(r, plan, sc.S3["now"])
    return r, j


def test_old_judgment_cannot_bypass_new_review():
    p = fresh(); r, old = s1(p)
    p.review(r, sc.S1["planning"], sc.S1["now"])
    blocked(lambda: p.judge.sign(old, "W002", sc.S1["now"]))


def test_old_judgment_cannot_resume_suspended_work():
    p = fresh(); r, j = s1(p)
    p.judge.sign(j, "W002", sc.S1["now"])
    p.con.execute("INSERT INTO sensor_readings(equipment_id,ts,current_a) VALUES('P-05',?,18)", (f"{D}T10:40",))
    p.monitor(j.permit_id, f"{D}T10:41")
    blocked(lambda: p.judge.sign(j, "W002", f"{D}T10:42"))
    assert p.con.execute("SELECT status FROM permits WHERE id=?", (j.permit_id,)).fetchone()[0] == "suspended"


def test_new_sensor_requires_review_before_signature():
    p = fresh(); r, j = s1(p)
    p.con.execute("INSERT INTO sensor_readings(equipment_id,ts,current_a) VALUES('P-05',?,8)", (f"{D}T10:31",))
    blocked(lambda: p.judge.sign(j, "W002", f"{D}T10:32"))


def test_sensor_expiry_requires_review_before_signature():
    p = fresh()
    p.con.execute("DELETE FROM sensor_readings WHERE equipment_id='P-05' AND ts>?", (f"{D}T10:30",))
    r, j = s1(p)
    blocked(lambda: p.judge.sign(j, "W002", f"{D}T11:15"))


def test_judgment_tampering_blocked():
    p = fresh(); r, j = s1(p)
    j.results[0].reason = "altered"
    blocked(lambda: p.judge.sign(j, "W002", sc.S1["now"]))


def test_double_signature_and_review_of_issued_blocked():
    p = fresh(); r, j = s1(p)
    p.judge.sign(j, "W002", sc.S1["now"])
    blocked(lambda: p.judge.sign(j, "W002", sc.S1["now"]))
    blocked(lambda: p.review(r, sc.S1["planning"], sc.S1["now"]))


def test_request_metadata_and_crew_replaced():
    p = fresh(); r, j = s1(p)
    r.update(start=f"{D}T12:00", end=f"{D}T13:00", workers=["W003"], task="수리")
    p.review(r, sc.S1["planning"], sc.S1["now"])
    row = p.con.execute("SELECT * FROM permits WHERE id=?", (j.permit_id,)).fetchone()
    assert row["start_ts"] == r["start"] and row["end_ts"] == r["end"] and row["task"] == "수리"
    assert [x[0] for x in p.con.execute("SELECT worker_id FROM permit_workers WHERE permit_id=?", (j.permit_id,))] == ["W003"]


def test_unknown_work_type_blocked():
    p = fresh(); r = copy.deepcopy(sc.S1["request"]); r["work_type"] = "unsupported"
    blocked(lambda: p.review(r, sc.S1["planning"], sc.S1["now"]))


def test_empty_worker_list_blocked():
    p = fresh(); r = copy.deepcopy(sc.S1["request"]); r["workers"] = []
    blocked(lambda: p.review(r, sc.S1["planning"], sc.S1["now"]))


def test_missing_photo_cannot_update_lock():
    p = fresh()
    blocked(lambda: p.confirm_photo(999999, "P-03-E", True, f"{D}T13:10", "W002"))
    assert p.con.execute("SELECT locked FROM isolation_points WHERE id='P-03-E'").fetchone()[0] == 0


def photo(p):
    r = copy.deepcopy(sc.S2["request"])
    p.review(r, sc.S2["planning"], sc.S2["now"])
    img = (ROOT / "assets/samples/lock_and_tag.png").read_bytes()
    result = p.submit_photo_lock(r, "P-03-E", img, "lock.png", f"{D}T13:10")
    return r, result


def test_photo_confirmer_must_have_permission():
    p = fresh(); r, ev = photo(p)
    blocked(lambda: p.confirm_photo(ev["evidence_id"], "P-03-E", True, f"{D}T13:11", "W001"), PermissionError)


def test_photo_cannot_confirm_other_point():
    p = fresh(); r, ev = photo(p)
    blocked(lambda: p.confirm_photo(ev["evidence_id"], "P-03-H", True, f"{D}T13:11", "W002"))


def test_vlm_positive_still_requires_human_confirmation():
    p = fresh(); r = copy.deepcopy(sc.S2["request"])
    p.review(r, sc.S2["planning"], sc.S2["now"])
    original = L.read_lock_photo
    try:
        L.read_lock_photo = lambda *a: ({"padlock_present": True, "tag_name": "W001", "observation": "visible"}, "llm")
        ev = p.submit_photo_lock(r, "P-03-E", b"image", "sample.png", f"{D}T13:10")
    finally:
        L.read_lock_photo = original
    assert not ev["registry_updated"]
    assert p.con.execute("SELECT locked FROM isolation_points WHERE id='P-03-E'").fetchone()[0] == 0
    p.confirm_photo(ev["evidence_id"], "P-03-E", True, f"{D}T13:11", "W002")
    assert p.con.execute("SELECT locked FROM isolation_points WHERE id='P-03-E'").fetchone()[0] == 1


def test_loto_failure_stays_failure():
    p = fresh(); r, j = s1(p)
    p.submit_loto_check(r, "시동됨 / 잔압 방출 안 됨", f"{D}T10:31", "W001",
                         stored_energy_released=False, isolation_verified=False)
    j, _ = p.review(r, sc.S1["planning"], f"{D}T10:32")
    assert j.by_id("M-03").outcome == "reject" and j.by_id("M-04").outcome == "conditional"


def test_loto_missing_results_rejected():
    p = fresh(); r, j = s1(p)
    blocked(lambda: p.submit_loto_check(r, "방법", f"{D}T10:31", "W001"))


def test_unsigned_supplementary_does_not_satisfy_rule():
    p = fresh(); r = copy.deepcopy(sc.S3["request"])
    plan = copy.deepcopy(sc.S3["planning"]); plan["2"] = {"answer": "해당", "reason": "정전 병행"}
    p.review(r, plan, sc.S3["now"])
    assert p.submit_supplementary_permit(r, sc.S3["now"]) == "draft"
    j, _ = p.review(r, plan, sc.S3["now"])
    assert j.by_id("H-13").outcome == "conditional"
    blocked(lambda: p.submit_supplementary_permit(r, sc.S3["now"], "W001", verified=True), PermissionError)
    blocked(lambda: p.submit_supplementary_permit(r, sc.S3["now"], "W002"))
    p.submit_supplementary_permit(r, sc.S3["now"], "W002", verified=True)
    j, _ = p.review(r, plan, sc.S3["now"])
    assert j.by_id("H-13").outcome == "pass"


def test_partial_supplementary_coverage_not_sufficient():
    p = fresh(); r = copy.deepcopy(sc.S3["request"])
    plan = copy.deepcopy(sc.S3["planning"]); plan["2"] = {"answer": "해당", "reason": "정전 병행"}
    p.review(r, plan, sc.S3["now"])
    p.submit_supplementary_permit(r, sc.S3["now"], "W002", verified=True)
    p.con.execute("UPDATE permits SET end_ts=? WHERE id=?", (f"{D}T15:00", r["permit_id"] + "-PW"))
    j, _ = p.review(r, plan, sc.S3["now"])
    assert j.by_id("H-13").outcome == "conditional"


def test_null_sensor_monitor_does_not_crash():
    p = fresh(); r, j = s1(p); p.judge.sign(j, "W002", sc.S1["now"])
    p.con.execute("INSERT INTO sensor_readings(equipment_id,ts,current_a) VALUES('P-05',?,NULL)", (f"{D}T10:40",))
    alerts, _ = p.monitor(j.permit_id, f"{D}T10:41")
    assert any(a["level"] == "warn" for a in alerts)


def test_monitor_detects_unsafe_confined_air():
    p = fresh(); r, j = hot(p, confined=True)
    assert j.status == "approved"
    p.judge.sign(j, "W002", sc.S3["now"])
    p.con.execute("INSERT INTO gas_readings(zone_id,ts,lel_percent,o2,co2,co_ppm,h2s_ppm) "
                  "VALUES('W1',?,2,15,3,100,20)", (f"{D}T14:30",))
    alerts, _ = p.monitor(j.permit_id, f"{D}T14:31")
    assert any(a["level"] == "stop" and a["source"] == "SR-618" for a in alerts)
    assert p.con.execute("SELECT status FROM permits WHERE id=?", (j.permit_id,)).fetchone()[0] == "suspended"


def test_expired_work_closes_and_cannot_be_signed():
    p = fresh(); r, j = s1(p)
    blocked(lambda: p.judge.sign(j, "W002", f"{D}T11:30"))
    p.judge.sign(j, "W002", sc.S1["now"])
    p.monitor(j.permit_id, f"{D}T12:00")
    assert p.con.execute("SELECT status FROM permits WHERE id=?", (j.permit_id,)).fetchone()[0] == "closed"


def test_empty_human_decision_and_reason_blocked():
    p = fresh(); r, j = hot(p)
    p.con.execute("DELETE FROM gas_readings WHERE zone_id='W1'")
    j, _ = p.review(r, sc.S3["planning"], sc.S3["now"])
    for resolution in ({"decision": "", "note": ""}, {"decision": "accept", "note": ""}):
        blocked(lambda: p.judge.sign(j, "W002", sc.S3["now"], {"H-08": resolution}))


def test_malformed_llm_outputs_fall_back():
    class Broken:
        def chat(self, *args, **kwargs):
            return {}
    req = copy.deepcopy(sc.S1["request"])
    assert L.plan(Broken(), req, {})[1] == "fallback:error"
    assert L.inconsistencies(Broken(), req, {})[1] == "fallback:error"


def test_documents_use_saved_signature_and_current_status():
    p = fresh(); r, j = s1(p)
    p.judge.sign(j, "W002", sc.S1["now"])
    r["task"] = "changed client text"
    doc = p.documents(j, r)
    assert "changed client text" not in doc["permit_md"]
    assert sc.S1["now"].replace("T", " ") in doc["permit_md"]
    p.con.execute("INSERT INTO sensor_readings(equipment_id,ts,current_a) VALUES('P-05',?,18)", (f"{D}T10:40",))
    p.monitor(j.permit_id, f"{D}T10:41")
    assert "작업을 즉시 멈추십시오" in p.documents(j, r)["notice_ko"]


def test_unsigned_document_cannot_claim_issued():
    p = fresh(); r, j = s1(p)
    blocked(lambda: p.documents(j, r, "issued"))


def test_signature_ignores_harmless_na_to_pass_change():
    """서명 시 재판정에서 비적용→통과 변화는 '강화'가 아니므로 서명을 막지 않는다 (2026-10-04)."""
    p = fresh(); r, j = s1(p)
    na = next(x.rule_id for x in j.results if x.outcome == "n/a")
    original = p.judge.evaluate

    def evaluate(*args, **kw):
        cur = original(*args, **kw)
        for x in cur.results:
            if x.rule_id == na:
                x.outcome = "pass"
        return cur
    p.judge.evaluate = evaluate
    assert p.judge.sign(j, "W002", sc.S1["now"]) == "issued"


def test_signature_still_blocks_new_risk_at_signing():
    p = fresh(); r, j = s1(p)
    rid = next(x.rule_id for x in j.results if x.outcome == "pass")
    original = p.judge.evaluate

    def evaluate(*args, **kw):
        cur = original(*args, **kw)
        for x in cur.results:
            if x.rule_id == rid:
                x.outcome = "conditional"
        return cur
    p.judge.evaluate = evaluate
    blocked(lambda: p.judge.sign(j, "W002", sc.S1["now"]))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print("ok", test.__name__)
    print(f"{len(tests)}개 통과")
