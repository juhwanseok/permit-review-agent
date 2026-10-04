"""판정 엔진: 규칙 판정 → 집계 → (LLM 강화 조정) → 증빙 재검증 → 허가권자 서명.

설계 원칙 (permit_rules.yaml meta.design_principles)
- DP-01 LLM은 판정을 강화만 할 수 있다. LLM 판단은 '설계' 등급이라 최대 human_review까지다.
- DP-02 최종 서명은 허가권자(사람)가 한다.
- DP-03 데이터가 없으면 합격 처리하지 않고 human_review로 보낸다.
"""
import json
import hashlib
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .checks import CHECKS, Ctx
from .rules import RuleBook
from .tools import Tools

STATUS_BY_OUTCOME = {"reject": "rejected", "conditional": "conditional", "human_review": "human_review"}


@dataclass
class RuleResult:
    rule_id: str
    name: str
    outcome: str                  # pass | n/a | warn | human_review | conditional | reject
    grade: str
    source_id: str
    source_title: str
    clause: object = None
    conditions: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    origin: str = "rule"          # rule | planning | system | llm
    reason: str = ""


@dataclass
class Judgment:
    permit_id: str
    now: str
    status: str
    results: list
    missing_fields: list
    trace: list
    review_token: str = ""
    context: dict = field(default_factory=dict, repr=False)

    def by_id(self, rid):
        return next(r for r in self.results if r.rule_id == rid)

    def active(self):
        """통과·비적용을 뺀 결과 (화면·안내용)."""
        return [r for r in self.results if r.outcome not in ("pass", "n/a")]

    def conditions(self):
        return [c for r in self.results if r.outcome == "conditional" for c in r.conditions]


class Judge:
    def __init__(self, con: sqlite3.Connection, rulebook: RuleBook = None):
        self.con = con
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.rb = rulebook or RuleBook()
        missing = {r["id"] for r in self.rb.rules} ^ set(CHECKS)
        if missing:
            raise ValueError(f"규칙 파일과 판정 함수가 1:1로 대응하지 않음: {sorted(missing)}")

    # ── 신청 등록 ──
    def open_permit(self, req):
        old = self.con.execute("SELECT status, signed_at FROM permits WHERE id=?", (req["permit_id"],)).fetchone()
        if old and (old["signed_at"] or old["status"] in ("issued", "suspended", "closed")):
            raise ValueError("이미 확정·중지·종료된 허가입니다. 새 허가번호로 신청하세요")
        ptype = "화기작업" if req["work_type"] == "hot_work" else "일반위험작업"
        self.con.execute(
            "INSERT INTO permits (id, type, requester_id, equipment_id, zone_id, task, start_ts, end_ts, status, "
            "request_text, request_lang, request_json) VALUES (?,?,?,?,?,?,?,?,'reviewing',?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET type=excluded.type, requester_id=excluded.requester_id, "
            "equipment_id=excluded.equipment_id, zone_id=excluded.zone_id, task=excluded.task, "
            "start_ts=excluded.start_ts, end_ts=excluded.end_ts, request_text=excluded.request_text, "
            "request_lang=excluded.request_lang, request_json=excluded.request_json, status='reviewing', "
            "signed_by=NULL, signed_at=NULL",
            (req["permit_id"], ptype, req["requester"], req.get("equipment_id"), req["zone_id"], req["task"],
             req["start"], req["end"], req.get("request_text"), req.get("request_lang", "ko"),
             json.dumps(req, ensure_ascii=False)))
        self.con.execute("DELETE FROM permit_workers WHERE permit_id=?", (req["permit_id"],))
        for w in req.get("workers", []):
            self.con.execute("INSERT OR IGNORE INTO permit_workers VALUES (?,?,'작업자')", (req["permit_id"], w))
        if req.get("fire_watch"):
            self.con.execute("INSERT OR IGNORE INTO permit_workers VALUES (?,?,'화재감시자')",
                             (req["permit_id"], req["fire_watch"]))

    # ── 입력 검증 ──
    def validate_request(self, req):
        """엔진 입구 방어: 참조 무결성·필수 키·시각 형식. 문제가 있으면 ValueError (앱이 DB 오류로 멈추지 않게)."""
        problems = []
        if req.get("work_type") not in ("maintenance", "hot_work"):
            problems.append("지원하지 않는 작업 종류 (정비·화기작업만 지원)")
        if not isinstance(req.get("workers"), list) or not req.get("workers"):
            problems.append("작업자 명단이 비어 있거나 형식이 잘못됨")
        one = lambda sql, v: self.con.execute(sql, (v,)).fetchone()  # noqa: E731
        for k in ("permit_id", "work_type", "requester", "zone_id", "task", "start", "end"):
            if not req.get(k):
                problems.append(f"필수 항목 없음: {k}")
        if req.get("zone_id") and not one("SELECT 1 FROM zones WHERE id=?", req["zone_id"]):
            problems.append(f"등록되지 않은 구역: {req['zone_id']}")
        if req.get("equipment_id") and not one("SELECT 1 FROM equipment WHERE id=?", req["equipment_id"]):
            problems.append(f"등록되지 않은 설비: {req['equipment_id']}")
        for w in [req.get("requester")] + list(req.get("workers") or []) + [req.get("fire_watch")]:
            if w and not one("SELECT 1 FROM workers WHERE id=?", w):
                problems.append(f"등록되지 않은 작업자: {w}")
        for k in ("start", "end"):
            try:
                if req.get(k):
                    datetime.fromisoformat(req[k])
            except ValueError:
                problems.append(f"시각 형식 오류: {k}={req[k]}")
        if req.get("work_type") == "maintenance" and not req.get("equipment_id"):
            problems.append("정비작업인데 대상 설비가 없음")
        if problems:
            raise ValueError("신청 정보 확인 필요: " + "; ".join(problems))

    # ── 판정 ──
    def evaluate(self, req, planning, now, weather=None, persist=True) -> Judgment:
        self.validate_request(req)
        if persist:
            self.open_permit(req)
            self.con.execute("UPDATE permits SET planning_json=? WHERE id=?",
                             (json.dumps(planning, ensure_ascii=False), req["permit_id"]))
        sp02 = self.rb.params["SP-02"]["value"]["max_age_minutes"]
        tools = Tools(self.con, now, weather=weather, sensor_max_age_min=sp02)
        ctx = Ctx(tools, req, planning)
        results = []

        for rule in self.rb.rules:
            if req["work_type"] not in rule["work_types"]:
                continue
            chk = CHECKS[rule["id"]](ctx)
            src = self.rb.sources[rule["source"]]
            if chk.applies is False:
                outcome, reason = "n/a", ""
            elif chk.applies is None or chk.passed is None:
                outcome, reason = "human_review", "필요한 데이터 없음 (DP-03)"
            elif chk.passed:
                outcome, reason = "pass", ""
            else:
                outcome, reason = rule["on_fail"], ""
            results.append(RuleResult(
                rule["id"], rule["name"], outcome, src["grade"], rule["source"], src["title"],
                rule.get("clause"), rule.get("conditions", []) if outcome == "conditional" else [],
                chk.facts, "rule", reason))

        results += self._planning_results(planning)
        results += self._memory_results(req, tools, results)
        status = self._aggregate(results)
        j = Judgment(req["permit_id"], now, status, results, ctx.missing, tools.trace)
        j.context = {"request": json.loads(json.dumps(req)), "planning": json.loads(json.dumps(planning)),
                     "weather": dict(tools._weather)}
        if persist:
            self._audit(j)
        return j

    def _planning_results(self, planning):
        """C-C-49 6.1 14항목 중 '모름'·누락은 unknown_policy(human_review)."""
        pc, out = self.rb.planning, []
        unknown = [str(i["item"]) for i in pc["items"]
                   if planning.get(str(i["item"]), {}).get("answer") not in ("해당", "비해당")]
        if unknown:
            src = self.rb.sources[pc["source"]]
            out.append(RuleResult("PLAN", "허가 전 점검 14항목 미확정", pc["unknown_policy"], src["grade"],
                                  pc["source"], src["title"], pc["clause"], facts={"unknown_items": unknown},
                                  origin="planning", reason="확정되지 않은 점검 항목"))
        return out

    def _memory_results(self, req, tools, results):
        """SP-01: 30일 안에 진동 이상이 기준 횟수 이상이면 경고 (오늘 감지분 포함)."""
        if req["work_type"] != "maintenance":
            return []
        m09 = next((r for r in results if r.rule_id == "M-09"), None)
        if not m09 or m09.facts.get("zone") not in ("C", "D"):
            return []
        v = self.rb.params["SP-01"]["value"]
        n = tools.vib_events(req["equipment_id"], v["window_days"]) + 1
        if n < v["min_events"]:
            return []
        src = self.rb.sources["SYS"]
        return [RuleResult("SP-01", self.rb.params["SP-01"]["name"], "warn", src["grade"], "SYS", src["title"],
                           facts={"events_in_window": n, **v}, origin="system",
                           reason=f"{v['window_days']}일 안에 진동 이상 {n}회: 진동 원인 점검을 정비 범위에 포함 권고")]

    def _aggregate(self, results):
        for outcome in self.rb.order:          # 강한 판정부터
            if outcome in STATUS_BY_OUTCOME and any(r.outcome == outcome for r in results):
                return STATUS_BY_OUTCOME[outcome]
        return "approved"

    # ── LLM·모순 탐지 강화 조정 (DP-01) ──
    def apply_llm_adjustments(self, j: Judgment, adjustments) -> Judgment:
        """origin='rule'이면 결정적 모순 탐지(INC-xx), 그 외는 LLM(LLM-xx). 둘 다 설계 등급 상한."""
        cap = self.rb.cap("설계")
        for n, a in enumerate(adjustments, 1):
            if not a.get("reason"):
                raise ValueError("LLM 조정에는 이유가 필요하다")
            new = a["outcome"]
            if not self.rb.stronger(cap, new):
                raise ValueError(f"LLM 조정은 최대 '{cap}'까지다 (요청: {new})")
            if a.get("rule_id"):
                r = j.by_id(a["rule_id"])
                if not self.rb.stronger(new, r.outcome):
                    raise ValueError(f"{r.rule_id}: '{r.outcome}' → '{new}'는 완화다. LLM은 강화만 가능하다 (DP-01)")
                r.outcome, r.origin, r.reason = new, "llm", a["reason"]
            else:
                src = self.rb.sources["SYS"]
                origin = "system" if a.get("origin") == "rule" else "llm"
                prefix = "INC" if origin == "system" else "LLM"
                n_same = sum(1 for x in j.results if x.rule_id.startswith(prefix)) + 1
                j.results.append(RuleResult(f"{prefix}-{n_same:02d}", a.get("name", "복합위험 추론"), new, src["grade"],
                                            "SYS", src["title"], origin=origin, reason=a["reason"]))
        j.status = self._aggregate(j.results)
        self._audit(j, only_llm=True)
        return j

    # ── 증빙 (재검증 루프) ──
    def submit_evidence(self, permit_id, rule_id, now, image_path=None, value=None, vlm_result=None):
        cur = self.con.execute(
            "INSERT INTO evidence (permit_id, rule_id, image_path, value_json, vlm_result, submitted_at) "
            "VALUES (?,?,?,?,?,?)",
            (permit_id, rule_id, image_path, json.dumps(value, ensure_ascii=False) if value else None,
             vlm_result, now))
        self._log(permit_id, now, "증빙", None, rule_id, None, "submitted", vlm_result or image_path)
        return cur.lastrowid

    # ── 허가권자 서명 (DP-02) ──
    def sign(self, j: Judgment, signer_id, now, resolutions=None):
        # Serialize finalization with other SQLite writers; roll back only this operation.
        self.con.execute("SAVEPOINT final_signature")
        try:
            self.con.execute("UPDATE permits SET status=status WHERE id=?", (j.permit_id,))
            result = self._sign(j, signer_id, now, resolutions)
            self.con.execute("RELEASE final_signature")
            self.con.commit()
            return result
        except Exception:
            self.con.execute("ROLLBACK TO final_signature")
            self.con.execute("RELEASE final_signature")
            raise

    def _sign(self, j: Judgment, signer_id, now, resolutions=None):
        resolutions = resolutions or {}
        signer = self.con.execute("SELECT * FROM workers WHERE id=?", (signer_id,)).fetchone()
        if not signer or not signer["can_sign_permit"]:
            raise PermissionError(f"{signer_id}: 허가 서명 권한 없음")
        p = self.con.execute("SELECT * FROM permits WHERE id=?", (j.permit_id,)).fetchone()
        record = self.con.execute("SELECT detail FROM audit_log WHERE permit_id=? AND step='검토확정' "
                                  "ORDER BY id DESC LIMIT 1", (j.permit_id,)).fetchone()
        if not p or not record or p["signed_at"] or p["status"] != j.status:
            raise ValueError("최신 미서명 판정이 아닙니다. 재검토 또는 새 신청이 필요합니다")
        saved = json.loads(record[0])
        if (saved["token"] != j.review_token or saved["judgment"] != self._judgment_hash(j)
                or saved["state"] != self._state_hash()):
            raise ValueError("검토 후 신청·증빙·현장 상태가 변경되었습니다. 재검증하세요")
        sign_time = datetime.fromisoformat(now)
        if sign_time < datetime.fromisoformat(j.now):
            raise ValueError("서명 시각이 검토 시각보다 이릅니다")
        if j.status != "rejected" and sign_time >= datetime.fromisoformat(p["end_ts"]):
            raise ValueError("허가기간이 종료되었습니다. 새로 신청하세요")
        current = self.evaluate(j.context["request"], j.context["planning"], now,
                                weather=j.context["weather"], persist=False)
        previous = {r.rule_id: r.outcome for r in j.results}
        # 조치가 필요한 결과(reject·conditional·human_review·warn)로 바뀐 경우만 막는다. n/a→pass 같은 변화는 무시
        if any(r.outcome in self.rb.order and previous.get(r.rule_id) != r.outcome
               and self.rb.stronger(r.outcome, previous.get(r.rule_id, "pass")) for r in current.results):
            raise ValueError("서명 시점의 측정 유효성·판정이 달라졌습니다. 재검증하세요")
        if j.status == "conditional":
            raise ValueError("조건부 항목이 남아 있다. 증빙 제출 후 재판정해야 서명할 수 있다")
        if any(r.rule_id == "PLAN" and r.outcome == "human_review" for r in j.results):
            raise ValueError("허가 전 점검 14항목이 미확정이다. 신청 단계에서 확정한 뒤 재검증해야 서명할 수 있다 (L-S10)")
        if j.status == "rejected":
            final = "rejected"
        else:
            pending = [r.rule_id for r in j.results if r.outcome == "human_review" and r.rule_id not in resolutions]
            if pending:
                raise ValueError(f"사람 판단 항목 미결정: {pending}")
            allowed = {r.rule_id for r in j.results if r.outcome == "human_review"}
            for rid, v in resolutions.items():
                if (rid not in allowed or not isinstance(v, dict) or v.get("decision") not in ("accept", "reject")
                        or not isinstance(v.get("note"), str) or not v["note"].strip()):
                    raise ValueError("사람 판단에는 해당 항목의 수용/불수용 결정과 사유가 필요합니다")
            final = "rejected" if any(v["decision"] == "reject" for v in resolutions.values()) else "issued"
        self.con.execute("UPDATE permits SET status=?, signed_by=?, signed_at=? WHERE id=?",
                         (final, signer_id, now, j.permit_id))
        for rid, v in resolutions.items():
            self._log(j.permit_id, now, "서명", None, rid, None, v["decision"], v.get("note"))
        self._log(j.permit_id, now, "서명", None, None, None, final, signer_id)
        self._remember(j, now)
        return final

    def _remember(self, j, now):
        """기억: 진동 C/D영역 판정을 설비 이력에 남긴다 (서명 시 1회, 재판정 중복 방지)."""
        try:
            m09 = j.by_id("M-09")
        except StopIteration:
            return
        z = m09.facts.get("zone")
        if z in ("C", "D"):
            req = json.loads(self.con.execute("SELECT request_json FROM permits WHERE id=?",
                                              (j.permit_id,)).fetchone()[0])
            self.con.execute("INSERT INTO equipment_events (equipment_id, ts, event_type, detail) VALUES (?,?,?,?)",
                             (req["equipment_id"], now, f"vib_zone_{z.lower()}",
                              f"RMS {m09.facts['vib_rms']}mm/s, 허가 {j.permit_id} 검토 중 감지"))

    # ── 감사 로그 ──
    def _log(self, pid, ts, step, tool, rule_id, source_id, result, detail):
        self.con.execute("INSERT INTO audit_log (permit_id, ts, step, tool, rule_id, source_id, result, detail) "
                         "VALUES (?,?,?,?,?,?,?,?)", (pid, ts, step, tool, rule_id, source_id, result, detail))

    def _audit(self, j: Judgment, only_llm=False):
        if not only_llm:
            for t in j.trace:
                self._log(j.permit_id, j.now, "조회", t["tool"], None, None, None,
                          json.dumps(t["args"], ensure_ascii=False))
        for r in j.results:
            if r.outcome == "n/a" or (only_llm and r.origin not in ("llm", "system")):
                continue
            self._log(j.permit_id, j.now, "추론" if r.origin == "llm" else "판정", None, r.rule_id, r.source_id,
                      r.outcome, json.dumps(r.facts, ensure_ascii=False, default=str) if r.facts else r.reason)
        self.con.execute("UPDATE permits SET status=? WHERE id=?", (j.status, j.permit_id))
        j.review_token = uuid.uuid4().hex
        self._log(j.permit_id, j.now, "검토확정", None, None, None, j.status,
                  json.dumps({"token": j.review_token, "judgment": self._judgment_hash(j),
                              "state": self._state_hash()}, ensure_ascii=False))
        self.con.commit()

    @staticmethod
    def _judgment_hash(j):
        value = asdict(j)
        value.pop("review_token", None)
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

    def _state_hash(self):
        # This single-user demo conservatively invalidates review after any relevant DB change.
        names = ("site", "zones", "equipment", "isolation_points", "loto_checks", "procedures", "workers",
                 "trainings", "sensor_readings", "gas_readings", "permits", "permit_workers", "evidence", "equipment_events")
        value = {n: [dict(r) for r in self.con.execute(f'SELECT * FROM "{n}" ORDER BY rowid')] for n in names}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def to_dict(j: Judgment):
    return {**asdict(j), "results": [asdict(r) for r in j.results]}
