"""판정 엔진 테스트: 시연 시나리오 S1~S3 + Q7 변형 케이스 + 설계 원칙(DP-01~03)."""
import copy
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))
import seed  # noqa: E402
from engine import scenarios as sc  # noqa: E402
from engine.checks import CHECKS  # noqa: E402
from engine.judge import Judge  # noqa: E402
from engine.rules import RuleBook  # noqa: E402

RB = RuleBook()


def fresh():
    p = Path(tempfile.mkdtemp()) / "t.db"
    seed.build(p, date.fromisoformat(sc.DEMO_DATE))
    return sqlite3.connect(p)


def run(s, con=None, mutate_req=None, weather=None, planning=None):
    con = con or fresh()
    req = copy.deepcopy(s["request"])
    if mutate_req:
        mutate_req(req)
    judge = Judge(con, RB)
    return judge, judge.evaluate(req, planning or s["planning"], s["now"], weather=weather)


def outcome(j, rid):
    return j.by_id(rid).outcome


def expect_raises(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"{exc.__name__} 발생하지 않음")


# ── 구조 ──
def test_rules_and_checks_one_to_one():
    assert {r["id"] for r in RB.rules} == set(CHECKS)


# ── S1 ──
def test_s1_approved():
    _, j = run(sc.S1)
    assert j.status == "approved", [(r.rule_id, r.outcome) for r in j.active()]
    assert outcome(j, "M-01") == outcome(j, "M-02b") == outcome(j, "M-09") == "pass"
    assert outcome(j, "M-06") == "n/a" and outcome(j, "C-04") == "n/a"


def test_s1_sign_issues_permit():
    judge, j = run(sc.S1)
    assert judge.sign(j, "W002", sc.S1["now"]) == "issued"


def test_s1_variant_running_motor_rejected():
    con = fresh()
    con.execute("UPDATE sensor_readings SET current_a=12 WHERE equipment_id='P-05' AND ts=?", (sc.S1["now"],))
    _, j = run(sc.S1, con)
    assert outcome(j, "M-01") == "reject" and j.status == "rejected"


def test_s1_variant_stale_sensor_goes_to_human_dp03():
    con = fresh()
    con.execute("DELETE FROM sensor_readings WHERE equipment_id='P-05' AND ts>=?", (f"{sc.D}T10:00",))
    _, j = run(sc.S1, con)
    assert outcome(j, "M-01") == "human_review" and j.status == "human_review"


def test_s1_variant_heat_requires_rest_plan():
    hot = {"feels_like_max": 34.0, "warning_active": False}
    _, j = run(sc.S1, weather=hot)
    assert outcome(j, "C-04") == "conditional" and outcome(j, "C-05") == "pass"
    _, j = run(sc.S1, weather=hot, mutate_req=lambda r: r.update(rest_plan_2h_20m=True))
    assert outcome(j, "C-04") == "pass" and j.status == "approved"


def test_s1_variant_unknown_weather_dp03():
    _, j = run(sc.S1, weather={"feels_like_max": None, "warning_active": None})
    assert outcome(j, "C-04") == "human_review"


def test_planning_unknown_item_goes_to_human_and_blocks_signing():
    p = sc.plan({2, 7, 8})
    p["9"]["answer"] = "모름"
    judge, j = run(sc.S1, planning=p)
    assert outcome(j, "PLAN") == "human_review"
    expect_raises(ValueError, lambda: judge.sign(j, "W002", sc.S1["now"], {"PLAN": {"decision": "accept"}}))


# ── S2 ──
def test_s2_conditional_with_vibration_review_and_memory_warning():
    _, j = run(sc.S2)
    assert j.status == "conditional"
    assert outcome(j, "M-02a") == "pass"
    assert outcome(j, "M-02b") == "conditional"
    assert j.by_id("M-02b").facts["lockable_not_locked"] == ["P-03-E"]
    assert outcome(j, "M-09") == "human_review" and j.by_id("M-09").facts["zone"] == "C"
    assert outcome(j, "SP-01") == "warn" and j.by_id("SP-01").facts["events_in_window"] == 2
    assert j.conditions() == ["잠금장치 체결 사진 제출 → 재검증"]


def test_s2_cannot_sign_while_conditional():
    judge, j = run(sc.S2)
    expect_raises(ValueError, lambda: judge.sign(j, "W002", sc.S2["now"]))


def test_s2_evidence_then_reverify_then_sign():
    con = fresh()
    judge, j = run(sc.S2, con)
    ev = sc.S2["evidence"]
    # 작업자가 실제로 자물쇠를 채우고 LOTO 대장이 갱신됨 → 증빙 제출
    con.execute("UPDATE isolation_points SET locked=1, updated_at=? WHERE id=?", (ev["at"], ev["point"]))
    judge.submit_evidence(j.permit_id, ev["rule_id"], ev["at"], ev["image_path"], vlm_result=ev["vlm_result"])
    j2 = judge.evaluate(sc.S2["request"], sc.S2["planning"], ev["recheck_at"])
    assert any(t["tool"] == "loto_registry.points" for t in j2.trace)       # 처음부터 다시 조회
    assert outcome(j2, "M-02b") == "pass" and j2.status == sc.S2["expected_after_evidence"]
    # 잠금을 바꿨으니 12:45 차단 확인은 무효 → 다시 확인해야 함 (B-M-25 6.6)
    assert outcome(j2, "M-04") == "conditional" and not j2.by_id("M-04").facts["order_ok"]
    con.execute("INSERT INTO loto_checks (equipment_id, stored_energy_released, isolation_verified, verify_method, "
                "checked_by, checked_at) VALUES ('P-03',1,1,?, 'W001', ?)", (ev["verify_method"], ev["verify_at"]))
    j2 = judge.evaluate(sc.S2["request"], sc.S2["planning"], ev["final_at"])
    assert outcome(j2, "M-04") == "pass" and j2.status == sc.S2["expected_after_verify"]
    # 허가권자가 진동 항목을 판단해야 서명 가능
    expect_raises(ValueError, lambda: judge.sign(j2, "W002", ev["final_at"]))
    final = judge.sign(j2, "W002", ev["final_at"],
                       {"M-09": {"decision": "accept", "note": "금형 교체 중 슬라이드 가이드·베어링 점검 포함"}})
    assert final == "issued"
    n = con.execute("SELECT COUNT(*) FROM equipment_events WHERE equipment_id='P-03' AND event_type='vib_zone_c'"
                    ).fetchone()[0]
    assert n == 2                                                          # 기억에 오늘 감지분 저장


def test_s2_variant_no_lock_no_tag_rejected():
    con = fresh()
    con.execute("UPDATE isolation_points SET tagged=0 WHERE id='P-03-E'")
    _, j = run(sc.S2, con)
    assert outcome(j, "M-02a") == "reject" and j.status == "rejected"


def test_s2_variant_missing_field_listed_for_question():
    _, j = run(sc.S2, mutate_req=lambda r: r.pop("safety_block_used"))
    assert outcome(j, "M-06") == "human_review"
    assert "safety_block_used" in j.missing_fields


# ── S3 ──
def test_s3_rejected_by_two_legal_rules():
    _, j = run(sc.S3)
    assert j.status == "rejected"
    assert outcome(j, "H-01") == "reject" and j.by_id("H-01").grade == "법정"
    assert outcome(j, "H-11") == "reject" and j.by_id("H-11").facts["lacking"] == ["W003"]
    assert outcome(j, "X-01") == "human_review" and j.by_id("X-01").facts["clash"][0]["distance_m"] == 7.0
    assert outcome(j, "H-08") == "pass"
    assert j.by_id("H-01").facts["combustibles"][0]["zone"] == "PB"


def test_s3_variant_fixed_crew_leaves_only_human_review():
    def fix(r):
        r.update(workers=["W005"], fire_watch="W001", requester="W005")
    _, j = run(sc.S3, mutate_req=fix)
    assert outcome(j, "H-01") == outcome(j, "H-11") == "pass"
    assert j.status == "human_review" and [r.rule_id for r in j.active()] == ["X-01"]


def test_s3_variant_lel_12_conditional():
    con = fresh()
    con.execute("INSERT INTO gas_readings (zone_id, ts, lel_percent, o2) VALUES ('W2', ?, 12.0, 20.9)",
                (f"{sc.D}T13:45",))
    _, j = run(sc.S3, con, mutate_req=lambda r: r.update(workers=["W005"], fire_watch="W001"))
    assert outcome(j, "H-08") == "conditional" and j.status == "conditional"


def test_s3_variant_routine_booth_exempt():
    to_w1 = lambda r: r.update(zone_id="W1", equipment_id=None)  # noqa: E731
    _, j = run(sc.S3, mutate_req=to_w1)
    assert outcome(j, "H-01") == "n/a" and outcome(j, "H-11") == "n/a" and outcome(j, "X-01") == "pass"
    # W1은 오늘 가스 측정값이 없다 → 합격이 아니라 사람 판단 (DP-03)
    assert outcome(j, "H-08") == "human_review" and j.status == "human_review"
    con = fresh()
    con.execute("INSERT INTO gas_readings (zone_id, ts, lel_percent, o2) VALUES ('W1', ?, 2.0, 20.9)",
                (f"{sc.D}T13:30",))
    _, j = run(sc.S3, con, mutate_req=to_w1)
    assert j.status == "approved"


def test_h01_legal_reject_only_for_welding_and_cutting():
    # 제241조의2①: '용접ㆍ용단 작업' 대상. 연마·드릴은 자동 법정 반려가 아니라 사람 판단
    for task, want in [("용접", "reject"), ("용단", "reject"), ("연마", "human_review"), ("드릴", "human_review")]:
        _, j = run(sc.S3, mutate_req=lambda r: r.update(task=task))
        h01 = j.by_id("H-01")
        assert h01.outcome == want, (task, h01.outcome)
        assert (want == "reject") == ("용접·용단" not in h01.reason), (task, h01.reason)
        assert outcome(j, "H-11") == "reject" and j.status != "approved"   # 다른 화재위험작업 규칙은 그대로


def test_s3_missing_fire_watch_key_is_unknown_not_fail():
    _, j = run(sc.S3, mutate_req=lambda r: r.pop("fire_watch"))
    assert outcome(j, "H-01") == "human_review" and "fire_watch" in j.missing_fields


# ── 설계 원칙 ──
def test_dp01_llm_cannot_weaken():
    judge, j = run(sc.S2)
    expect_raises(ValueError, lambda: judge.apply_llm_adjustments(
        j, [{"rule_id": "M-02b", "outcome": "pass", "reason": "표지로 충분해 보임"}]))


def test_dp01_llm_cannot_reject():
    judge, j = run(sc.S1)
    expect_raises(ValueError, lambda: judge.apply_llm_adjustments(
        j, [{"rule_id": "M-09", "outcome": "reject", "reason": "소리가 이상함"}]))


def test_dp01_llm_can_escalate_to_human_review():
    judge, j = run(sc.S3)
    judge.apply_llm_adjustments(j, [{"name": "신청 기재와 현장 데이터 불일치", "outcome": "human_review",
                                     "reason": "신청서 '가연물 없음' vs 배치도·당일허가상 7m 도장 작업"}])
    assert j.by_id("LLM-01").origin == "llm" and j.status == "rejected"
    judge, j = run(sc.S1)
    judge.apply_llm_adjustments(j, [{"rule_id": "M-09", "outcome": "human_review", "reason": "이상음 원인 미확인"}])
    assert j.status == "human_review"


def test_dp02_only_authorized_signer():
    judge, j = run(sc.S1)
    expect_raises(PermissionError, lambda: judge.sign(j, "W001", sc.S1["now"]))


def test_audit_log_has_sources():
    con = fresh()
    run(sc.S3, con)
    rows = con.execute("SELECT rule_id, source_id, result FROM audit_log WHERE step='판정' AND result='reject'"
                       ).fetchall()
    assert {(r[0], r[1]) for r in rows} == {("H-01", "SR-241-2"), ("H-11", "EDU-B5")}


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
        print("ok", f.__name__)
    print(f"{len(fns)}개 통과")
