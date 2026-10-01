"""평가셋 실행: python tests/run_eval.py [llm|fallback|both] [출력폴더]

지표
- 구조화 필드 정확도 : stated 대비 (채점 필드 18개 × 케이스, "ANY" 제외)
- 환각               : 신청문에 없는데 값을 채운 필드 수
- 판정 일치율        : 파이프라인 최종 판정 == gold (truth로 엔진을 돌린 결과)
- 오승인             : gold가 승인이 아닌데 승인이 나온 건수 (안전상 가장 중요, 목표 0)
- 과잉 제한          : gold보다 엄격하게 나온 건수
- 계획 정확도        : LLM이 채운 14항목 칸 중 정답과 같은 비율
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
sys.path.insert(0, str(ROOT / "tests"))
import seed  # noqa: E402
from engine import llm as L  # noqa: E402
from engine import scenarios as sc  # noqa: E402
from engine.pipeline import Pipeline  # noqa: E402
from eval_cases import CASES, HOLDOUT, SCORED  # noqa: E402

D = sc.DEMO_DATE
STRICT = {"approved": 0, "human_review": 1, "conditional": 2, "rejected": 3}


def fresh(client):
    p = Path(tempfile.mkdtemp()) / "e.db"
    seed.build(p, date.fromisoformat(D))
    return Pipeline(sqlite3.connect(p), client)


def truth_raw(t):
    r = {k: None for k in L.STRUCT_SCHEMA["properties"]}
    r.update(extra_workers=[], summary_ko="")
    r.update({k: v for k, v in t.items() if k in r})
    return r


def field_value(req, k):
    """req 기준 값. fire_watch: 키 없음='(언급없음)', None='없음'."""
    if k == "extra_workers":
        return req.get("workers", [])[1:]
    if k == "fire_watch":
        return "(언급없음)" if "fire_watch" not in req else ("없음" if req["fire_watch"] is None else req["fire_watch"])
    return req.get(k)


def expected_value(stated, k):
    if k == "extra_workers":
        return stated.get(k, [])
    if k == "fire_watch":
        return stated.get(k, "(언급없음)")
    return stated.get(k)


def plan_truth(case):
    return {str(i): {"answer": "해당" if i in case["plan"] else "비해당", "reason": "[사람 확정]"} for i in range(1, 15)}


def gold_status(case):
    pl = fresh(None)
    # gold는 사람이 확정한 truth이므로 신청문 키워드 교차 확인을 거치지 않는다 (text="")
    req = pl.to_request(truth_raw(case["truth"]), case["requester"], "", case["lang"], D, case["id"])
    pl.answer(req, "crew", case["truth"].get("extra_workers", []))
    j, _ = pl.review(req, plan_truth(case), f"{D}T{case['now']}")
    return j.status, [(r.rule_id, r.outcome) for r in j.active()]


def run_case(case, client):
    pl = fresh(client)
    now = f"{D}T{case['now']}"
    t0 = time.time()
    raw, smode = pl.structure(case["text"], case["requester"])
    req = pl.to_request(raw, case["requester"], case["text"], case["lang"], D, case["id"])
    fields = {}
    for k in SCORED:
        exp = expected_value(case["stated"], k)
        if exp == "ANY":
            continue
        got = field_value(req, k)
        fields[k] = {"ok": got == exp, "got": got, "exp": exp,
                     "hallucination": exp in (None, [], "(언급없음)") and got not in (None, [], "(언급없음)")}
    asked, rounds = [], 0
    while rounds < 5:
        qs = pl.pending_questions(req)
        answerable = [q for q in qs if q["field"] in case["truth"] or q["field"] == "crew"]
        if not answerable:
            break
        for q in answerable:
            asked.append(q["field"])
            v = case["truth"].get("extra_workers", []) if q["field"] == "crew" else case["truth"][q["field"]]
            pl.answer(req, q["field"], v)
        rounds += 1
    unanswered = [q["field"] for q in pl.pending_questions(req)]
    truth_plan = plan_truth(case)
    try:
        planning, pmode = pl.plan(req, now)
        llm_items = {k: v["answer"] == truth_plan[k]["answer"] for k, v in planning.items() if v["reason"].startswith("[LLM]")}
        data_items = {k: v["answer"] == truth_plan[k]["answer"] for k, v in planning.items() if v["reason"].startswith("[데이터]")}
        unknown = [k for k, v in planning.items() if v["answer"] == "모름"]
        for k in unknown:
            planning[k] = truth_plan[k]
        j, extra = pl.review(req, planning, now)
        status, active, err = j.status, [(r.rule_id, r.outcome) for r in j.active()], None
    except ValueError as e:
        pmode, llm_items, data_items, unknown, status, active, err = None, {}, {}, [], "error", [], str(e)
        extra = {"findings": []}
    return {"struct_mode": smode, "fields": fields, "notes": req.get("_notes", []), "asked": asked,
            "unanswered": unanswered, "plan_mode": pmode, "plan_llm_items": llm_items, "plan_data_items": data_items,
            "plan_unknown": unknown, "status": status, "active": active, "error": err,
            "findings": [f["claim"] for f in extra["findings"]], "seconds": round(time.time() - t0, 1)}


def summarize(results):
    n = len(results)
    f_all = [f for r in results for f in r["fields"].values()]
    halluc = [(r["id"], k, f["got"]) for r in results for k, f in r["fields"].items() if f["hallucination"]]
    wrong = [(r["id"], k, f["got"], f["exp"]) for r in results for k, f in r["fields"].items() if not f["ok"]]
    status_ok = [r for r in results if r["status"] == r["gold"]]
    false_approve = [r["id"] for r in results if r["status"] == "approved" and r["gold"] != "approved"]
    stricter = [r["id"] for r in results if r["status"] in STRICT and STRICT[r["status"]] > STRICT[r["gold"]]]
    looser = [r["id"] for r in results if r["status"] in STRICT and STRICT[r["status"]] < STRICT[r["gold"]]]
    llm_plan = [v for r in results for v in r["plan_llm_items"].values()]
    data_plan = [v for r in results for v in r["plan_data_items"].values()]
    exact = [r["id"] for r in results if all(f["ok"] for f in r["fields"].values())]
    return {
        "cases": n,
        "field_accuracy": f"{sum(f['ok'] for f in f_all)}/{len(f_all)} ({100 * sum(f['ok'] for f in f_all) / len(f_all):.1f}%)",
        "exact_structuring": f"{len(exact)}/{n}",
        "hallucinations": len(halluc), "hallucination_list": halluc,
        "wrong_fields": wrong,
        "status_match": f"{len(status_ok)}/{n} ({100 * len(status_ok) / n:.1f}%)",
        "false_approvals": false_approve, "looser_than_gold": looser, "stricter_than_gold": stricter,
        "errors": [(r["id"], r["error"]) for r in results if r["error"]],
        "plan_llm_accuracy": f"{sum(llm_plan)}/{len(llm_plan)}" if llm_plan else "-",
        "plan_data_accuracy": f"{sum(data_plan)}/{len(data_plan)}" if data_plan else "-",
        "mean_seconds": round(sum(r["seconds"] for r in results) / n, 1),
        "questions_asked_mean": round(sum(len(r["asked"]) for r in results) / n, 1),
    }


def main(which="both", out_dir=None, subset="dev"):
    global CASES
    if subset == "holdout":
        CASES = HOLDOUT
    out_dir = Path(out_dir or ROOT / "tests" / "eval_out")
    out_dir.mkdir(parents=True, exist_ok=True)
    golds = {}
    label_mismatch = []
    for c in CASES:
        golds[c["id"]] = gold_status(c)
        if golds[c["id"]][0] != c["label"]:
            label_mismatch.append((c["id"], c["label"], golds[c["id"]]))
    print("라벨 점검 (label ≠ gold):", label_mismatch or "없음")
    modes = {"fallback": None}
    if which in ("llm", "both"):
        cl = L.OllamaClient()
        if not cl.available():
            print("LLM 미연결: fallback만 실행")
        else:
            cl.warmup()
            modes = {"llm": cl} if which == "llm" else {"llm": cl, "fallback": None}
    report = {"date": time.strftime("%Y-%m-%d %H:%M"), "label_mismatch": label_mismatch, "modes": {}}
    for name, client in modes.items():
        results = []
        for c in CASES:
            r = run_case(c, client)
            r.update(id=c["id"], tag=c["tag"], gold=golds[c["id"]][0], gold_active=golds[c["id"]][1])
            results.append(r)
            mark = "OK " if r["status"] == r["gold"] else "XX "
            print(f"[{name}] {mark}{c['id']} {c['tag']:<10} gold={r['gold']:<12} got={r['status']:<12} "
                  f"fields {sum(f['ok'] for f in r['fields'].values())}/{len(r['fields'])} {r['seconds']}s")
        report["modes"][name] = {"summary": summarize(results), "results": results}
        print(name, json.dumps(report["modes"][name]["summary"], ensure_ascii=False, indent=1))
    (out_dir / "eval_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("저장:", out_dir / "eval_result.json")


if __name__ == "__main__":
    main(*(sys.argv[1:4]))
