"""검사기가 잘못된 규칙을 실제로 잡아내는지 확인하는 음성 테스트."""
import copy
import sys
from pathlib import Path

import yaml

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"
sys.path.insert(0, str(RULES_DIR))
from validate_rules import validate  # noqa: E402

DOC = yaml.safe_load((RULES_DIR / "permit_rules.yaml").read_text(encoding="utf-8"))


def errs(mutate):
    doc = copy.deepcopy(DOC)
    mutate(doc)
    return validate(doc)[0]


def rule(doc, rid):
    return next(r for r in doc["rules"] if r["id"] == rid)


def test_current_file_passes():
    assert validate(DOC)[0] == []


def test_rule_without_source_rejected():
    assert errs(lambda d: rule(d, "M-01").pop("source"))


def test_kosha_rule_cannot_reject():
    # 2026-09-29 결정: KOSHA 권고 근거(LEL 10%)는 조건부 승인까지만
    assert errs(lambda d: rule(d, "H-08").update(on_fail="reject"))


def test_design_value_cannot_reject():
    assert errs(lambda d: rule(d, "X-01").update(on_fail="reject"))


def test_out_of_scope_task_cannot_reject():
    # 제241조의2 대상(용접·용단) 밖 작업의 판정은 설계 판단 → 사람 판단까지만
    assert errs(lambda d: rule(d, "H-01").update(out_of_scope_on_fail="reject"))


def test_conditional_needs_conditions():
    assert errs(lambda d: rule(d, "M-02b").pop("conditions"))


def test_unknown_clause_rejected():
    assert errs(lambda d: rule(d, "H-08").update(clause="9.9"))


def test_dangling_trigger_rejected():
    assert errs(lambda d: d["planning_checklist"]["items"][0]["triggers"].append("H-99"))


def test_duplicate_id_rejected():
    assert errs(lambda d: d["rules"].append(copy.deepcopy(rule(d, "M-01"))))


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
        print("ok", f.__name__)
    print(f"{len(fns)}개 통과")
