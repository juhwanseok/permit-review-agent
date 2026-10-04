"""permit_rules.yaml 무결성 검사.

- 모든 규칙·설정값에 존재하는 source가 있는가 (출처 없는 규칙 로드 거부)
- on_fail 판정 강도가 근거 등급 상한(grade_policy)을 넘지 않는가
- ID 중복, planning_checklist 트리거가 실제 규칙을 가리키는가
- conditional 규칙에 conditions가 있는가
실행: python validate_rules.py [permit_rules.yaml]
"""
import sys
from collections import Counter
from pathlib import Path

import yaml


def validate(doc):
    errors, warnings = [], []
    meta, sources = doc["meta"], doc["sources"]
    order = meta["action_order"]
    policy = meta["grade_policy"]

    for sid, s in sources.items():
        if s.get("grade") not in policy:
            errors.append(f"{sid}: 알 수 없는 등급 {s.get('grade')!r}")

    rules = doc["rules"]
    ids = [r["id"] for r in rules]
    for rid, n in Counter(ids).items():
        if n > 1:
            errors.append(f"{rid}: ID 중복 {n}회")

    for r in rules:
        rid, sid = r["id"], r.get("source")
        if sid not in sources:
            errors.append(f"{rid}: 출처 없음/미등록 source={sid!r}")
            continue
        grade = sources[sid]["grade"]
        cap, action = policy[grade], r.get("on_fail")
        if action not in order:
            errors.append(f"{rid}: 알 수 없는 판정 {action!r}")
        elif order.index(action) < order.index(cap):
            errors.append(f"{rid}: 판정 '{action}'이 {grade} 등급 상한 '{cap}'을 초과")
        if "task_scope" in r:
            # 조문 대상 작업 밖의 판정은 설계 판단이므로 설계 등급 상한을 넘을 수 없다
            oos, design_cap = r.get("out_of_scope_on_fail"), policy["설계"]
            if not isinstance(r["task_scope"], list) or not r["task_scope"]:
                errors.append(f"{rid}: task_scope는 작업 목록이어야 함")
            if oos not in order or order.index(oos) < order.index(design_cap):
                errors.append(f"{rid}: out_of_scope_on_fail {oos!r}이 설계 등급 상한 '{design_cap}'을 초과")
        if action == "conditional" and not r.get("conditions"):
            errors.append(f"{rid}: conditional인데 conditions 없음")
        clauses = sources[sid].get("clauses")
        if clauses:
            cited = r.get("clause")
            cited = cited if isinstance(cited, list) else [cited]
            for c in cited:
                if c not in clauses:
                    errors.append(f"{rid}: clause {c!r}가 {sid} 조항 목록에 없음")
        for b in r.get("basis", []):
            if b not in sources:
                errors.append(f"{rid}: basis {b!r} 미등록")

    rule_ids = set(ids)
    pc = doc["planning_checklist"]
    if pc["source"] not in sources:
        errors.append("planning_checklist: 출처 미등록")
    for item in pc["items"]:
        for t in item["triggers"]:
            if t not in rule_ids:
                errors.append(f"체크리스트 {item['item']}번: 트리거 {t} 규칙 없음")

    for p in doc.get("system_params", []):
        if p.get("source") not in sources:
            errors.append(f"{p['id']}: 출처 미등록")
    return errors, warnings


def summary(doc):
    sources = doc["sources"]
    by = Counter((sources[r["source"]]["grade"], r["on_fail"]) for r in doc["rules"])
    print(f"규칙 {len(doc['rules'])}개 · 출처 {len(sources)}개 · 설정값 {len(doc.get('system_params', []))}개")
    for (g, a), n in sorted(by.items()):
        print(f"  {g:<6} {a:<13} {n}")


if __name__ == "__main__":
    path = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("permit_rules.yaml"))
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors, warnings = validate(doc)
    summary(doc)
    for w in warnings:
        print("경고:", w)
    for e in errors:
        print("오류:", e)
    print("통과" if not errors else f"실패 ({len(errors)}건)")
    sys.exit(1 if errors else 0)
