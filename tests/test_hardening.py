"""견고성 회귀 테스트 (2026-09-30 기술 점검에서 재현한 버그 A1~A9, 속도 개선 B1~B3)."""
import sqlite3
import sys
import tempfile
from datetime import date
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))
import seed  # noqa: E402
from engine import llm as L  # noqa: E402
from engine import scenarios as sc  # noqa: E402
from engine.pipeline import Pipeline, normalize_hhmm  # noqa: E402

D = sc.DEMO_DATE


def fresh(client=None):
    p = Path(tempfile.mkdtemp()) / "t.db"
    seed.build(p, date.fromisoformat(D))
    return Pipeline(sqlite3.connect(p), client)


def raw(**kw):
    r = {k: None for k in L.STRUCT_SCHEMA["properties"]}
    r.update(work_type="maintenance", task="점검", extra_workers=[], summary_ko="", equipment_id="P-05",
             zone_id="L1", start_hhmm="10:30", end_hhmm="11:30", body_in_danger_zone=False)
    r.update(kw)
    return r


HOT = dict(work_type="hot_work", task="용접", equipment_id=None, zone_id="W1", fire_watch="W001",
           extinguisher_placed=True, spark_barrier="인증방화포", ventilation_done=True,
           excess_combustibles_removed=True, post_work_fire_watch_min=30, start_hhmm="15:00", end_hhmm="16:00")


def expect_raises(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"{exc.__name__} 발생하지 않음")


# ── A1·A2: LLM 출력 ID 정정 ──
def test_a1_equipment_name_mapped_to_id():
    pl = fresh()
    req = pl.to_request(raw(equipment_id="3호기"), "W001", "t", "ko", D, "P1")
    assert req["equipment_id"] == "P-03" and any("3호기" in n for n in req["_notes"])


def test_a1_unknown_equipment_becomes_question_not_crash():
    pl = fresh()
    req = pl.to_request(raw(equipment_id="없는기계"), "W001", "t", "ko", D, "P1")
    assert req["equipment_id"] is None
    assert "equipment_id" in [q["field"] for q in pl.pending_questions(req)]


def test_a2_worker_and_fire_watch_names_mapped():
    pl = fresh()
    req = pl.to_request(raw(**{**HOT, "fire_watch": "최용접"}, extra_workers=["김정비", "모르는사람"]),
                        "W003", "t", "vi", D, "P2")
    assert req["fire_watch"] == "W005" and req["workers"] == ["W003", "W001"]
    assert any("모르는사람" in n for n in req["_notes"])


def test_a2_fire_watch_none_words():
    pl = fresh()
    for word in ("없음", "không có", "none"):
        req = pl.to_request(raw(**{**HOT, "fire_watch": word}), "W003", "t", "vi", D, "P3")
        assert "fire_watch" in req and req["fire_watch"] is None


def test_engine_rejects_unregistered_refs_with_clear_error():
    pl = fresh()
    req = pl.to_request(raw(), "W001", "t", "ko", D, "P4")
    req["workers"] = ["W999"]
    expect_raises(ValueError, lambda: pl.review(req, sc.S1["planning"], f"{D}T10:30"))


# ── A3: LLM 오류 → 폴백 ──
class Boom(L.OllamaClient):
    def chat(self, *a, **k):
        self.calls.append({"purpose": k.get("purpose"), "seconds": None, "error": "TimeoutError"})
        raise TimeoutError("응답 없음")


def test_a3_every_llm_function_falls_back_on_error():
    b = Boom()
    pl = fresh(b)
    r, mode = pl.structure("3호기 금형 교체 13시~15시", "W001")
    assert mode == "fallback:error" and r["equipment_id"] == "P-03"
    req = pl.to_request(r, "W001", "t", "ko", D, "P5")
    planning, pmode = pl.plan(req, f"{D}T13:00")
    assert pmode == "fallback:error" and planning["7"]["answer"] == "해당"
    found, imode = L.inconsistencies(b, {"claimed_no_combustibles": True},
                                     {"combustibles_within_11m": [{"name": "PB", "distance_m": 7, "types": "x"}]})
    assert imode == "fallback:error" and found and found[0]["by"] == "rule"
    assert L.translate(b, "안내", "vi") == (None, "fallback:error")
    assert L.read_lock_photo(b, b"x", "P-03") == (None, "fallback:error")


# ── A4~A6: 시각 ──
def test_a4_time_normalization():
    cases = {"10:30": "10:30", "9:00": "09:00", "14시": "14:00", "14시 30분": "14:30", "오후 2시": "14:00",
             "오전 10시": "10:00", "14h30": "14:30", "25:00": None, "오전 중": None, "1030": None}
    assert {k: normalize_hhmm(k) for k in cases} == cases


def test_a4_unparseable_time_becomes_question():
    pl = fresh()
    req = pl.to_request(raw(start_hhmm="오전 중"), "W001", "t", "ko", D, "P6")
    assert "start" not in req and "start_hhmm" in [q["field"] for q in pl.pending_questions(req)]


def test_a5_a6_c02_time_order_and_shift():
    for s, e, bad in (("11:30", "10:30", "종료가 시작"), ("06:00", "07:00", "근무 시작"), ("16:00", "18:00", "근무 종료")):
        pl = fresh()
        req = pl.to_request(raw(start_hhmm=s, end_hhmm=e), "W001", "t", "ko", D, "P7")
        j, _ = pl.review(req, sc.S1["planning"], f"{D}T10:30")
        r = j.by_id("C-02")
        assert r.outcome == "conditional" and any(bad in p for p in r.facts["problems"]), (s, e, r.facts)


# ── A7: 오래된 가스 측정값 ──
def test_a7_stale_gas_reading_not_accepted():
    pl = fresh()
    pl.con.execute("INSERT INTO gas_readings (zone_id, ts, lel_percent, o2) VALUES ('W1', ?, 2, 20.9)", (f"{D}T08:30",))
    req = pl.to_request(raw(**HOT), "W005", "t", "ko", D, "P8")
    j, _ = pl.review(req, sc.S3["planning"], f"{D}T14:50")
    assert j.by_id("H-08").outcome == "conditional" and j.by_id("H-08").facts["stale"]
    pl.submit_gas(req, 2.0, 20.9, f"{D}T14:55", "W002")                     # 작업 직전 재측정
    j, _ = pl.review(req, sc.S3["planning"], f"{D}T14:56")
    assert j.by_id("H-08").outcome == "pass"


# ── A8: 감시 중 데이터 끊김 ──
def test_a8_monitor_warns_on_sensor_gap_and_missing_gas():
    pl = fresh()
    req = pl.to_request(raw(), "W001", "t", "ko", D, "P9")
    j, _ = pl.review(req, sc.S1["planning"], f"{D}T10:30")
    pl.judge.sign(j, "W002", f"{D}T10:30")
    pl.con.execute("DELETE FROM sensor_readings WHERE equipment_id='P-05' AND ts > ?", (f"{D}T10:30",))
    alerts, _ = pl.monitor(j.permit_id, f"{D}T11:20")
    assert alerts and alerts[0]["level"] == "warn" and "끊김" in alerts[0]["text"]


# ── A9: 차단 확인은 잠금 변경 이후여야 ──
def test_a9_isolation_check_must_follow_last_lock_change():
    pl = fresh()
    req = pl.to_request(raw(equipment_id="P-03", task="금형교체", start_hhmm="13:00", end_hhmm="15:00",
                            body_in_danger_zone=True, safety_block_used=True, die_precheck_completed=6),
                        "W001", "t", "ko", D, "PA")
    j, _ = pl.review(req, sc.S2["planning"], f"{D}T13:00")
    assert j.by_id("M-04").outcome == "pass"                                # 12:45 확인 ≥ 12:40 잠금
    pl.con.execute("UPDATE isolation_points SET locked=1, updated_at=? WHERE id='P-03-E'", (f"{D}T13:10",))
    j, _ = pl.review(req, sc.S2["planning"], f"{D}T13:12")
    assert j.by_id("M-04").outcome == "conditional" and j.by_id("M-04").facts["order_ok"] is False


# ── B1~B3: 호출 설정 ──
def test_b1_b2_chat_body_has_keep_alive_and_num_ctx():
    sent = {}

    class Spy(L.OllamaClient):
        def chat(self, system, user, schema, images=None, purpose=""):
            import json as _j
            body = {"keep_alive": L.KEEP_ALIVE, "options": {"temperature": 0, "num_ctx": L.NUM_CTX}}
            sent.update(body)
            return _j.loads(_j.dumps({"text": "ok"}))
    Spy().chat("s", "u", {})
    assert L.KEEP_ALIVE == "30m" and L.NUM_CTX == 8192
    src = (ROOT / "engine" / "llm.py").read_text(encoding="utf-8")
    assert '"keep_alive": KEEP_ALIVE' in src and '"num_ctx": NUM_CTX' in src


def test_b3_image_is_shrunk():
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (3000, 2000), "white").save(buf, format="PNG")
    out = L._shrink_image(buf.getvalue())
    assert max(Image.open(BytesIO(out)).size) == L.MAX_IMAGE_PX
    small = (ROOT / "assets/samples/lock_and_tag.png").read_bytes()
    assert L._shrink_image(small) == small                                  # 이미 작으면 그대로


def test_crew_confirmation_catches_missed_coworker():
    """신청문에서 동료를 놓쳐도 명단 확인 질문으로 미이수자가 드러나야 한다 (평가셋 E08)."""
    pl = fresh()
    req = pl.to_request(raw(start_hhmm="11:00", end_hhmm="12:00"), "W001", "5호기 점검, 이도장 씨랑", "ko", D, "PC")
    assert "crew" in [q["field"] for q in pl.pending_questions(req)]
    pl.answer(req, "crew", ["W004", "W999", "W001"])                       # 없는 ID·신청자 중복은 무시
    assert req["workers"] == ["W001", "W004"]
    j, _ = pl.review(req, sc.S1["planning"], f"{D}T10:50")
    assert j.by_id("M-08").outcome == "reject" and j.by_id("M-08").facts["lacking"] == ["W004"]


def test_crosscheck_fixes_observed_llm_errors():
    """평가셋에서 Gemma 4가 실제로 낸 오류를 그대로 넣어 교차 확인이 바로잡는지 (E16·E23·E24, 화재감시자 중복)."""
    pl = fresh()
    r = pl.to_request(raw(**{**HOT, "task": "드릴"}), "W005", "용접 전용 부스에서 브래킷 용접 14:00~15:00", "ko", D, "X1")
    assert r["task"] == "용접" and r["zone_id"] == "W1"
    r = pl.to_request(raw(work_type="hot_work", task="점검", equipment_id=None, zone_id=None), "W005",
                      "절단 작업 좀 할게요.", "ko", D, "X2")
    assert r["task"] == "용단" and r["work_type"] == "hot_work"
    r = pl.to_request(raw(equipment_id="P-03", zone_id="L1", task="금형교체"), "W001",
                      "3호기 금형 교체를 2라인에서 13~15시에 합니다.", "ko", D, "X3")
    assert r["zone_id"] == "W2"                                               # 명시된 장소 → C-01로 불일치 검사
    r = pl.to_request(raw(**HOT, extra_workers=["W001"]), "W005", "용접, 화재감시자 김정비", "ko", D, "X4")
    assert r["workers"] == ["W005"] and r["fire_watch"] == "W001"
    # 사람 이름 속 키워드는 무시 (보류셋 H04: '최용접')
    r = pl.to_request(raw(equipment_id="P-05", task="점검"), "W001", "5호기 점검, 최용접이랑 같이", "ko", D, "X6")
    assert r["task"] == "점검" and r["work_type"] == "maintenance"
    # 약한 키워드는 LLM이 '기타'일 때만 (보류셋 H01·H10)
    r = pl.to_request(raw(task="기타"), "W001", "5호기 그리스 주입", "ko", D, "X7")
    assert r["task"] == "점검"
    # 키워드가 두 가지 이상이면 정정하지 않는다
    r = pl.to_request(raw(task="금형교체", equipment_id="P-03"), "W001", "금형 교체하고 설치 전 검사도 함", "ko", D, "X5")
    assert r["task"] == "금형교체"


# ── 2026-10-04 실사용 점검에서 찾은 문제 ──
def test_training_alternative_names_lacking_and_excludes_crew():
    """대안이 이미 작업 중인 신청자를 '교체 후보'로 내지 않고, 미이수자를 밝혀야 한다."""
    pl = fresh()
    req = pl.to_request(raw(start_hhmm="11:00", end_hhmm="12:00"), "W001", "5호기 점검", "ko", D, "PA")
    pl.answer(req, "crew", ["W004"])
    j, extra = pl.review(req, sc.S1["planning"], f"{D}T10:50")
    alt = next(a["text"] for a in extra["alternatives"] if a["rule"] == "M-08")
    assert "이도장" in alt and "김정비" not in alt and "이수자 없음" in alt


def test_other_day_request_is_out_of_scope():
    """'내일' 작업을 오늘 허가로 처리하지 않는다 (허가는 당일 한정, C-C-49 5.6)."""
    pl = fresh()
    for text in ("내일 오전 9시부터 12시까지 1호기 수리합니다.", "Ngày mai sửa máy 1", "Fix press 1 tomorrow"):
        req = pl.to_request(raw(equipment_id="P-01", task="수리"), "W001", text, "ko", D, "PD")
        assert req.get("_out_of_scope") and pl.pending_questions(req) == []
        expect_raises(ValueError, lambda: pl.review(req, sc.S1["planning"], f"{D}T10:30"))
    req = pl.to_request(raw(), "W001", "5호기 점검 10:30~11:30", "ko", D, "PD2")
    assert not req.get("_out_of_scope")


def test_maintenance_without_equipment_can_answer_none():
    """설비가 없는 정비는 질문에서 '설비 없음'을 고를 수 있고, 허가 대상 아님으로 안내한다."""
    from engine.pipeline import NO_EQUIPMENT
    pl = fresh()
    req = pl.to_request(raw(equipment_id=None, zone_id="MS", task="청소"), "W001", "자재창고 선반 정리", "ko", D, "PE")
    assert "equipment_id" in [q["field"] for q in pl.pending_questions(req)]
    pl.answer(req, "equipment_id", NO_EQUIPMENT)
    assert pl.pending_questions(req) == [] and "대상이 아닙니다" in req["_out_of_scope"]
    expect_raises(ValueError, lambda: pl.review(req, sc.S1["planning"], f"{D}T10:30"))


def test_questions_in_english():
    pl = fresh()
    req = pl.to_request(raw(start_hhmm=None, end_hhmm=None), "W001", "Inspect press 5", "en", D, "PF")
    qs = {q["field"]: q["question"] for q in pl.pending_questions(req)}
    assert qs["start_hhmm"] == "Work start time?" and qs["crew"].startswith("Select")


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
        print("ok", f.__name__)
    print(f"{len(fns)}개 통과")
