"""시나리오 실행 로그 출력.  python -m engine.demo [S1|S2|S3|all]"""
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "data"))
import seed  # noqa: E402

from . import scenarios as sc  # noqa: E402
from .judge import Judge  # noqa: E402

MARK = {"reject": "✖ 반려", "conditional": "△ 조건부", "human_review": "? 사람판단", "warn": "! 경고",
        "pass": "✓", "n/a": "-"}
STATUS = {"approved": "승인 (서명 대기)", "conditional": "조건부 승인", "human_review": "사람 판단 필요",
          "rejected": "반려", "issued": "허가서 발행", }


def show(j, title):
    print(f"\n━━ {title}  [{j.now[11:]}] ━━")
    for t in j.trace:
        args = ", ".join(f"{k}={v}" for k, v in t["args"].items())
        print(f"  조회  {t['tool']}({args})")
    for r in j.results:
        if r.outcome == "n/a":
            continue
        src = f"{r.source_id}" + (f" {r.clause}" if r.clause and not isinstance(r.clause, list) else "")
        line = f"  판정  {MARK[r.outcome]:<8} {r.rule_id:<6} {r.name}  〔{r.grade}·{src}〕"
        if r.outcome != "pass":
            detail = r.reason or ", ".join(f"{k}={v}" for k, v in r.facts.items())
            line += f"\n                    └ {detail}"
        print(line)
    if j.missing_fields:
        print(f"  되묻기 필요: {j.missing_fields}")
    for c in j.conditions():
        print(f"  요구 조치: {c}")
    print(f"  ▶ 결과: {STATUS.get(j.status, j.status)}")


def main(which="all"):
    p = Path(tempfile.mkdtemp()) / "demo.db"
    seed.build(p, date.fromisoformat(sc.DEMO_DATE))
    con = sqlite3.connect(p)
    judge = Judge(con)

    if which in ("S1", "all"):
        s = sc.S1
        j = judge.evaluate(s["request"], s["planning"], s["now"])
        show(j, "S1 5호기 점검")
        print(f"  ▶ 서명(박안전): {STATUS[judge.sign(j, 'W002', s['now'])]}")

    if which in ("S2", "all"):
        s, ev = sc.S2, sc.S2["evidence"]
        j = judge.evaluate(s["request"], s["planning"], s["now"])
        show(j, "S2 3호기 금형교체 (1차)")
        con.execute("UPDATE isolation_points SET locked=1, updated_at=? WHERE id=?", (ev["at"], ev["point"]))
        judge.submit_evidence(j.permit_id, ev["rule_id"], ev["at"], ev["image_path"], vlm_result=ev["vlm_result"])
        print(f"\n  증빙 제출 [{ev['at'][11:]}] {ev['image_path']} → VLM: {ev['vlm_result']}")
        j = judge.evaluate(s["request"], s["planning"], ev["recheck_at"])
        show(j, "S2 재검증 1 (잠금 반영)")
        con.execute("INSERT INTO loto_checks (equipment_id, stored_energy_released, isolation_verified, "
                    "verify_method, checked_by, checked_at) VALUES ('P-03',1,1,?,'W001',?)",
                    (ev["verify_method"], ev["verify_at"]))
        print(f"\n  차단 재확인 [{ev['verify_at'][11:]}] {ev['verify_method']}")
        j = judge.evaluate(s["request"], s["planning"], ev["final_at"])
        show(j, "S2 재검증 2 (차단 확인)")
        final = judge.sign(j, "W002", ev["final_at"], {"M-09": {"decision": "accept",
                                                                    "note": "금형 교체 중 베어링 점검 포함"}})
        print(f"  ▶ 서명(박안전, M-09 판단 포함): {STATUS[final]}")

    if which in ("S3", "all"):
        s = sc.S3
        j = judge.evaluate(s["request"], s["planning"], s["now"])
        show(j, "S3 2라인 용접")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")
