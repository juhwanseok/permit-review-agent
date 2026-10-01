"""파이프라인 테스트 (LLM 없이 폴백 모드). 시나리오 S1~S3을 자유 형식 신청문부터 끝까지 돌린다."""
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
from engine.pipeline import Pipeline  # noqa: E402

D = sc.DEMO_DATE


def fresh():
    p = Path(tempfile.mkdtemp()) / "t.db"
    seed.build(p, date.fromisoformat(D))
    return Pipeline(sqlite3.connect(p), client=None)


def intake(pl, s, answers=None):
    """신청문 → 구조화 → 되묻기 응답 → request."""
    r = s["request"]
    raw, mode = pl.structure(r["request_text"], r["requester"])
    req = pl.to_request(raw, r["requester"], r["request_text"], r.get("request_lang", "ko"), D, r["permit_id"])
    answers = dict(answers or {})
    crew = answers.pop("crew", None)
    for f, v in answers.items():
        pl.answer(req, f, v)
    if crew is not None:
        pl.answer(req, "crew", crew)
    return req, mode


def test_fallback_structures_s2_text():
    pl = fresh()
    req, mode = intake(pl, sc.S2)
    assert mode == "fallback"
    assert req["equipment_id"] == "P-03" and req["task"] == "금형교체" and req["work_type"] == "maintenance"
    assert req["start"] == f"{D}T13:00" and req["end"] == f"{D}T15:00"
    qs = [q["field"] for q in pl.pending_questions(req)]
    assert qs == ["body_in_danger_zone", "die_precheck_completed", "crew"]


def test_questions_follow_answers():
    pl = fresh()
    req, _ = intake(pl, sc.S2, {"body_in_danger_zone": True})
    assert [q["field"] for q in pl.pending_questions(req)] == ["safety_block_used", "die_precheck_completed", "crew"]


def test_fallback_structures_s3_vietnamese_and_asks_in_vietnamese():
    pl = fresh()
    req, _ = intake(pl, sc.S3)
    assert req["work_type"] == "hot_work" and req["zone_id"] == "W2" and req["claimed_no_combustibles"]
    qs = pl.pending_questions(req)
    assert qs[0]["field"] == "fire_watch" and "giám sát cháy" in qs[0]["question"]


def test_s1_end_to_end_with_question_for_vague_time():
    pl = fresh()
    req, _ = intake(pl, sc.S1)
    fields = [q["field"] for q in pl.pending_questions(req)]
    assert "start_hhmm" in fields and "end_hhmm" in fields         # '오전 중'은 모호 → 되묻기
    pl.answer(req, "start_hhmm", "10:30")
    pl.answer(req, "end_hhmm", "11:30")
    assert [q["field"] for q in pl.pending_questions(req)] == ["crew"]      # 작업자 명단은 항상 확인
    pl.answer(req, "crew", [])
    assert pl.pending_questions(req) == []
    planning, _ = pl.plan(req, sc.S1["now"])
    j, extra = pl.review(req, planning, sc.S1["now"])
    # 데이터 기반 계획은 판단 불가 항목을 '모름'으로 둔다 → 사람 확인
    assert j.by_id("PLAN").outcome == "human_review"
    assert planning["2"]["answer"] == "해당" and planning["7"]["answer"] == "해당"   # 전기 차단점·유압 데이터
    j, _ = pl.review(req, sc.S1["planning"], sc.S1["now"])        # 사람이 14항목을 확정한 경우
    assert j.status == "approved"


def test_s3_contradiction_escalated_and_alternatives():
    pl = fresh()
    ans = {"fire_watch": "없음", "extinguisher_placed": True, "spark_barrier": "인증방화포", "ventilation_done": True,
           "excess_combustibles_removed": True, "post_work_fire_watch_min": 30}
    req, _ = intake(pl, sc.S3, ans)
    j, extra = pl.review(req, sc.S3["planning"], sc.S3["now"])
    assert j.status == "rejected"
    assert extra["findings"][0]["related_rule"] == "H-06" and "도장 부스 7.0m" in extra["findings"][0]["evidence"]
    inc = j.by_id("INC-01")
    assert inc.origin == "system" and inc.outcome == "human_review"          # 결정적 모순 탐지는 LLM과 구분
    alts = {a["rule"]: a["text"] for a in extra["alternatives"]}
    assert "최용접" in alts["H-11"] and "17:00" in alts["X-01"] and "익일" in alts["X-01"]


def test_s2_photo_without_vlm_needs_human_confirm_then_passes():
    pl = fresh()
    req, _ = intake(pl, sc.S2, {"body_in_danger_zone": True, "safety_block_used": True, "die_precheck_completed": 6})
    j, _ = pl.review(req, sc.S2["planning"], sc.S2["now"])
    assert j.by_id("M-02b").outcome == "conditional"
    img = (ROOT / "assets/samples/lock_and_tag.png").read_bytes()
    res = pl.submit_photo_lock(req, "P-03-E", img, "lock_and_tag.png", f"{D}T13:10")
    assert res["mode"] == "fallback" and not res["registry_updated"]
    j, _ = pl.review(req, sc.S2["planning"], f"{D}T13:11")
    assert j.by_id("M-02b").outcome == "conditional"                    # 확인 전에는 그대로
    pl.confirm_photo(res["evidence_id"], "P-03-E", True, f"{D}T13:12", "W002")
    j, _ = pl.review(req, sc.S2["planning"], f"{D}T13:12")
    assert j.by_id("M-02b").outcome == "pass" and j.by_id("M-04").outcome == "conditional"   # 잠금 후 재확인 필요
    pl.submit_loto_check(req, "잠금 후 시동 시도 → 무반응", f"{D}T13:14", "W001")
    j, _ = pl.review(req, sc.S2["planning"], f"{D}T13:15")
    assert j.by_id("M-04").outcome == "pass" and j.status == "human_review"


def test_gas_evidence_clears_h08():
    pl = fresh()
    pl.con.execute("INSERT INTO gas_readings (zone_id, ts, lel_percent, o2) VALUES ('W2', ?, 12, 20.9)", (f"{D}T13:45",))
    ans = {"fire_watch": "W001", "extinguisher_placed": True, "spark_barrier": "인증방화포", "ventilation_done": True,
           "excess_combustibles_removed": True, "post_work_fire_watch_min": 30}
    req, _ = intake(pl, sc.S3, ans)
    req["workers"] = ["W005"]
    j, _ = pl.review(req, sc.S3["planning"], sc.S3["now"])
    assert j.by_id("H-08").outcome == "conditional"
    pl.submit_gas(req, 4.0, 20.9, f"{D}T13:55", "W002")
    j, _ = pl.review(req, sc.S3["planning"], f"{D}T13:56")
    assert j.by_id("H-08").outcome == "pass"


def test_field_evidence_and_documents():
    pl = fresh()
    req, _ = intake(pl, sc.S2, {"body_in_danger_zone": True, "safety_block_used": True, "die_precheck_completed": 3})
    j, _ = pl.review(req, sc.S2["planning"], sc.S2["now"])
    assert j.by_id("M-07").outcome == "conditional"
    pl.submit_field(req, "M-07", f"{D}T13:05")
    j, _ = pl.review(req, sc.S2["planning"], f"{D}T13:06")
    assert j.by_id("M-07").outcome == "pass"
    docs = pl.documents(j, req)
    assert "별지 양식 2" in docs["permit_md"] and "M-02b" in docs["permit_md"]
    assert "해야 할 일" in docs["notice_ko"]


def test_monitor_suspends_on_machine_start():
    pl = fresh()
    req, _ = intake(pl, sc.S1, {"start_hhmm": "10:30", "end_hhmm": "11:30"})
    j, _ = pl.review(req, sc.S1["planning"], sc.S1["now"])
    assert pl.judge.sign(j, "W002", sc.S1["now"]) == "issued"
    alerts, _ = pl.monitor(j.permit_id, f"{D}T10:40")
    assert alerts == []
    pl.con.execute("INSERT INTO sensor_readings (equipment_id, ts, current_a, vib_rms) VALUES ('P-05', ?, 18, 2.0)",
                   (f"{D}T10:45",))
    alerts, _ = pl.monitor(j.permit_id, f"{D}T10:46")
    assert alerts[0]["level"] == "stop"
    assert pl.con.execute("SELECT status FROM permits WHERE id=?", (j.permit_id,)).fetchone()[0] == "suspended"


def test_monitor_hot_work_gas_and_post_watch():
    pl = fresh()
    ans = {"fire_watch": "W001", "extinguisher_placed": True, "spark_barrier": "인증방화포", "ventilation_done": True,
           "excess_combustibles_removed": True, "post_work_fire_watch_min": 30}
    req, _ = intake(pl, sc.S3, ans)
    req.update(zone_id="W1", equipment_id=None, workers=["W005"], claimed_no_combustibles=False)
    pl.con.execute("INSERT INTO gas_readings (zone_id, ts, lel_percent, o2) VALUES ('W1', ?, 2, 20.9)", (f"{D}T13:40",))
    j, _ = pl.review(req, sc.S3["planning"], sc.S3["now"])
    assert j.status == "approved"
    pl.judge.sign(j, "W002", sc.S3["now"])
    alerts, _ = pl.monitor(j.permit_id, f"{D}T16:10")
    assert any("화재감시 중" in a["text"] for a in alerts)
    pl.con.execute("INSERT INTO gas_readings (zone_id, ts, lel_percent, o2) VALUES ('W1', ?, 11, 20.9)", (f"{D}T14:30",))
    alerts, _ = pl.monitor(j.permit_id, f"{D}T14:31")
    assert alerts[0]["level"] == "stop"


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
        print("ok", f.__name__)
    print(f"{len(fns)}개 통과")
