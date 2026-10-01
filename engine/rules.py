"""규칙 파일 로드. 무결성 검사를 통과하지 못하면 엔진을 시작하지 않는다."""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "rules" / "permit_rules.yaml"
sys.path.insert(0, str(ROOT / "rules"))
from validate_rules import validate  # noqa: E402


class RuleBook:
    def __init__(self, path: Path = RULES_PATH):
        self.doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        errors, _ = validate(self.doc)
        if errors:
            raise ValueError("규칙 파일 검사 실패: " + "; ".join(errors))
        self.meta = self.doc["meta"]
        self.sources = self.doc["sources"]
        self.rules = self.doc["rules"]
        self.by_id = {r["id"]: r for r in self.rules}
        self.params = {p["id"]: p for p in self.doc.get("system_params", [])}
        self.order = self.meta["action_order"]          # [reject, conditional, human_review, warn]
        self.planning = self.doc["planning_checklist"]

    def grade(self, rule):
        return self.sources[rule["source"]]["grade"]

    def cap(self, grade):
        return self.meta["grade_policy"][grade]

    def stronger(self, a, b):
        """a가 b보다 강하거나 같은가. pass/n/a는 가장 약하다."""
        rank = {k: i for i, k in enumerate(self.order + ["pass", "n/a"])}
        return rank[a] <= rank[b]
