"""Gemma 4 실모델 점검 (느림, 수동 실행). python tests/llm_smoke.py [출력.json]

S1~S3 신청문을 LLM으로 구조화 → 계획 → 판정 → 모순 탐지 → 번역, 그리고 증빙 사진 2장 판독.
기대값과 다르면 MISMATCH로 표시한다 (실패해도 끝까지 돈다). 결과는 JSON으로 저장.
"""
import json
import sqlite3
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))
import seed  # noqa: E402
from engine import llm as L  # noqa: E402
from engine import scenarios as sc  # noqa: E402
from engine.pipeline import Pipeline  # noqa: E402

D = sc.DEMO_DATE
EXPECT = {
    "S1": {"equipment_id": "P-05", "task": "점검", "work_type": "maintenance", "status": "approved"},
    "S2": {"equipment_id": "P-03", "task": "금형교체", "work_type": "maintenance", "status": "conditional"},
    "S3": {"zone_id": "W2", "work_type": "hot_work", "claimed_no_combustibles": True, "status": "rejected"},
}
ANSWERS = {  # 되묻기에 대한 사람 응답 (시나리오 정의)
    "S1": {"start_hhmm": "10:30", "end_hhmm": "11:30", "body_in_danger_zone": False},
    "S2": {"body_in_danger_zone": True, "safety_block_used": True, "die_precheck_completed": 6},
    "S3": {"fire_watch": "없음", "extinguisher_placed": True, "spark_barrier": "인증방화포", "ventilation_done": True,
           "excess_combustibles_removed": True, "post_work_fire_watch_min": 30},
}


def main(out_path):
    client = L.OllamaClient()
    if not client.available():
        print("Ollama/gemma4 미연결")
        return 1
    p = Path(tempfile.mkdtemp()) / "t.db"
    seed.build(p, date.fromisoformat(D))
    pl = Pipeline(sqlite3.connect(p), client)
    report = {"model": client.model, "date": time.strftime("%Y-%m-%d %H:%M"), "scenarios": {}, "vision": {}}
    for name, s in sc.ALL.items():
        r = s["request"]
        raw, _ = pl.structure(r["request_text"], r["requester"])
        req = pl.to_request(raw, r["requester"], r["request_text"], r.get("request_lang", "ko"), D, r["permit_id"])
        asked = []
        for _ in range(4):                                  # 되묻기 여러 라운드
            qs = [q["field"] for q in pl.pending_questions(req) if q["field"] in ANSWERS[name] or q["field"] == "crew"]
            if not qs:
                break
            asked += qs
            for f in qs:
                pl.answer(req, f, ANSWERS[name].get(f, []))
        still = [q["field"] for q in pl.pending_questions(req)]
        planning, pmode = pl.plan(req, s["now"])
        auto_unknown = [k for k, x in planning.items() if x["answer"] == "모름"]
        j_auto, _ = pl.review(req, planning, s["now"])
        # 사람이 표에서 '모름' 항목을 확정 (시나리오 정답 사용)
        for k in auto_unknown:
            planning[k] = dict(s["planning"][k], reason="[사람 확정]")
        j, extra = pl.review(req, planning, s["now"])
        docs = pl.documents(j, req)
        tr, _ = pl.translate(docs["notice_ko"], "vi") if name == "S3" else (None, None)
        got = {**{k: req.get(k) for k in EXPECT[name] if k != "status"}, "status": j.status}
        report["scenarios"][name] = {
            "structured": raw, "asked": asked, "unanswered": still,
            "planning": planning, "plan_mode": pmode, "unknown_after_llm": auto_unknown,
            "status_before_human_plan": j_auto.status, "status": j.status,
            "active": [(x.rule_id, x.outcome, x.reason) for x in j.active()],
            "findings": extra["findings"], "notice_vi": tr,
            "check": {k: ("OK" if got[k] == v else f"MISMATCH (got {got[k]!r})") for k, v in EXPECT[name].items()},
        }
        print(name, report["scenarios"][name]["check"])
    for img, expect in (("lock_and_tag.png", True), ("tag_only.png", False)):
        v, _ = L.read_lock_photo(client, (ROOT / "assets/samples" / img).read_bytes(), "P-03 주전원 차단기")
        report["vision"][img] = {**v, "check": "OK" if v["padlock_present"] == expect else "MISMATCH"}
        print(img, report["vision"][img]["check"])
    report["calls"] = [{"purpose": c["purpose"], "seconds": c["seconds"]} for c in client.calls]
    Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("저장:", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "llm_smoke_result.json"))
