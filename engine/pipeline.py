"""전체 흐름 조립: 신청 → 구조화 → 되묻기 → 계획 → 판정 → 모순 강화 → 대안 → 증빙 → 재검증 → 서명 → 발행 → 감시.

GUI(app.py)와 테스트가 같은 함수를 쓴다. LLM 클라이언트가 None이면 결정적 폴백으로 동작한다.
"""
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta

from . import llm as L
from .checks import DIE_INSTALL_TASKS, DIE_TASKS
from .judge import Judge, Judgment

# ─────────────────────────────────────────────
# 되묻기: 필드별 질문 (한국어·베트남어·영어 템플릿. 질문 문구는 LLM이 만들지 않는다)
# ─────────────────────────────────────────────
FIELD_SPECS = {
    "equipment_id": {"type": "equipment", "ko": "작업 대상 설비는 무엇입니까?", "vi": "Bạn sẽ làm việc trên thiết bị nào?"},
    "zone_id": {"type": "zone", "ko": "작업 장소(구역)는 어디입니까?", "vi": "Khu vực làm việc ở đâu?"},
    "start_hhmm": {"type": "time", "ko": "작업 시작 시각은?", "vi": "Giờ bắt đầu làm việc?"},
    "end_hhmm": {"type": "time", "ko": "작업 종료 시각은?", "vi": "Giờ kết thúc làm việc?"},
    "body_in_danger_zone": {"type": "bool", "ko": "슬라이드 아래 등 위험한계 안으로 손이나 몸이 들어갑니까?",
                            "vi": "Tay hoặc cơ thể có đi vào vùng nguy hiểm (dưới đầu trượt của máy dập) không?"},
    "safety_block_used": {"type": "bool", "ko": "안전블록을 사용합니까?", "vi": "Có dùng khối chặn an toàn không?"},
    "die_precheck_completed": {"type": "int06", "ko": "금형 설치 전 검사 6항목 중 몇 개를 완료했습니까?",
                               "vi": "Đã hoàn thành bao nhiêu mục trong 6 mục kiểm tra trước khi lắp khuôn?"},
    "fire_watch": {"type": "worker_or_none", "ko": "화재감시자는 누구입니까? (없으면 '없음')",
                   "vi": "Ai là người giám sát cháy? (nếu không có, chọn 'Không có')"},
    "extinguisher_placed": {"type": "bool", "ko": "작업 장소에 소화기를 비치했습니까?",
                            "vi": "Đã đặt bình chữa cháy tại nơi làm việc chưa?"},
    "spark_barrier": {"type": "choice", "choices": L.SPARK_BARRIER_CHOICES,
                      "ko": "불티 비산방지 조치는 무엇입니까?", "vi": "Biện pháp chống tia lửa bắn là gì?"},
    "ventilation_done": {"type": "bool", "ko": "인화성 가스가 남지 않도록 환기했습니까?",
                         "vi": "Đã thông gió để không còn khí dễ cháy chưa?"},
    "excess_combustibles_removed": {"type": "bool", "ko": "작업에 필요한 양 외의 가연물을 치웠습니까?",
                                    "vi": "Đã dọn vật dễ cháy không cần thiết chưa?"},
    "post_work_fire_watch_min": {"type": "minutes", "ko": "작업 종료 후 몇 분간 화재감시를 합니까?",
                                 "vi": "Sau khi xong việc sẽ giám sát cháy bao nhiêu phút?"},
    "gas_remeasure_plan_4h": {"type": "bool", "ko": "4시간마다 가스농도를 재측정합니까?",
                              "vi": "Có đo lại nồng độ khí mỗi 4 giờ không?"},
    # 작업자 명단은 특별교육 판정(M-08·H-11)의 대상이다. 신청문에서 놓치면 미이수자가 빠진 채 승인될 수 있어 항상 확인.
    "crew": {"type": "crew", "ko": "신청자 외에 함께 작업하는 사람을 모두 선택하세요 (없으면 비워 두세요).",
             "vi": "Chọn tất cả những người cùng làm việc ngoài bạn (nếu không có, để trống)."},
}
FIELD_EN = {
    "equipment_id": "Which equipment will you work on?",
    "zone_id": "Where is the work area (zone)?",
    "start_hhmm": "Work start time?",
    "end_hhmm": "Work end time?",
    "body_in_danger_zone": "Will your hands or body enter the danger zone (e.g. under the press slide)?",
    "safety_block_used": "Will you use a safety block?",
    "die_precheck_completed": "How many of the 6 pre-installation die checks are done?",
    "fire_watch": "Who is the fire watch? (choose 'None' if nobody)",
    "extinguisher_placed": "Is a fire extinguisher placed at the work site?",
    "spark_barrier": "What spark protection will you use?",
    "ventilation_done": "Has the area been ventilated so no flammable gas remains?",
    "excess_combustibles_removed": "Have combustibles not needed for the work been removed?",
    "post_work_fire_watch_min": "How many minutes of fire watch after the work?",
    "gas_remeasure_plan_4h": "Will gas concentration be re-measured every 4 hours?",
    "crew": "Select everyone working with you (leave empty if nobody).",
}
for _k, _v in FIELD_EN.items():
    FIELD_SPECS[_k]["en"] = _v
FIELD_LABEL_KO = {k: v["ko"] for k, v in FIELD_SPECS.items()}

# 허가는 작업 당일에만 신청·발급한다(C-C-49 5.6). 다른 날 작업을 오늘 날짜로 잘못 처리하지 않도록 막는다.
OTHER_DAY = re.compile(r"내일|모레|다음\s*주|다음\s*날|익일|tomorrow|next\s+week|ngày\s*mai|ngày\s*kia|tuần\s*sau", re.I)
NO_EQUIPMENT = "없음"
OUT_OF_SCOPE = {
    "other_day": "허가는 작업 당일에만 신청·발급합니다(KOSHA C-C-49 5.6). 다른 날 작업은 그날 다시 신청하세요.",
    "no_equipment": "대상 설비가 없는 작업은 정비작업 허가(기준규칙 제92조 등) 대상이 아닙니다. "
                    "용접·용단 등 화기작업이면 신청 내용에 작업 종류를 적어 다시 신청하세요.",
}
STATUS_KO = {"approved": "승인(서명 대기)", "conditional": "조건부 승인", "human_review": "사람 판단 필요",
             "rejected": "반려", "issued": "발행", "suspended": "작업 정지", "closed": "종료"}
OUTCOME_KO = {"pass": "통과", "reject": "반려", "conditional": "조건부", "human_review": "사람 판단", "warn": "경고",
              "n/a": "비적용"}


def required_fields(req):
    """판정 전에 채워야 할 필드 (작업 종류·작업 내용에 따라 달라짐)."""
    f = ["zone_id", "start_hhmm", "end_hhmm"]
    if req.get("work_type") == "maintenance":
        f.append("equipment_id")
        if req.get("task") in DIE_TASKS:
            f += ["body_in_danger_zone"]
            if req.get("body_in_danger_zone"):
                f += ["safety_block_used"]
        if req.get("task") in DIE_INSTALL_TASKS:
            f += ["die_precheck_completed"]
    else:
        f += ["fire_watch", "extinguisher_placed", "spark_barrier", "ventilation_done",
              "excess_combustibles_removed", "post_work_fire_watch_min"]
    return f + ["crew"]


class Pipeline:
    def __init__(self, con: sqlite3.Connection, client=None):
        self.con = con
        con.row_factory = sqlite3.Row
        self.client = client
        self.judge = Judge(con)

    @property
    def mode(self):
        return "llm" if self.client else "fallback"

    # ── 참조 데이터 ──
    def context(self):
        q = lambda s: [dict(r) for r in self.con.execute(s).fetchall()]  # noqa: E731
        return {"equipment": q("SELECT id, name, type, zone_id FROM equipment ORDER BY id"),
                "zones": q("SELECT id, name FROM zones ORDER BY id"),
                "workers": q("SELECT id, name, role, language, can_sign_permit FROM workers ORDER BY id")}

    def worker(self, wid):
        r = self.con.execute("SELECT * FROM workers WHERE id=?", (wid,)).fetchone()
        return dict(r) if r else None

    # ── 1. 구조화 ──
    def structure(self, text, requester):
        raw, mode = L.structure(self.client, text, requester, self.context())
        return raw, mode

    def sanitize(self, raw):
        """LLM 출력을 DB 기준으로 검증·정정한다. 목록에 없는 ID는 이름으로 찾아 맞추고, 못 찾으면 null(되묻기).

        반환: (정정된 raw, 정정 메모 목록). 메모는 화면과 감사 로그에 남긴다 (LLM 출력을 그대로 믿지 않음).
        """
        raw, notes = dict(raw), []
        ctx = self.context()
        eq_ids = {e["id"] for e in ctx["equipment"]}
        zone_ids = {z["id"] for z in ctx["zones"]}
        by_name = {w["name"]: w["id"] for w in ctx["workers"]}
        w_ids = {w["id"] for w in ctx["workers"]}

        def worker_id(v):
            if v in w_ids:
                return v
            if v in by_name:
                return by_name[v]
            hits = [wid for name, wid in by_name.items() if name in str(v)]
            return hits[0] if len(hits) == 1 else None

        e = raw.get("equipment_id")
        if e is not None and e not in eq_ids:
            m = re.search(r"(\d+)\s*호기", str(e))
            cand = f"P-{int(m.group(1)):02d}" if m else next(
                (x["id"] for x in ctx["equipment"] if x["name"] in str(e) or str(e) in x["name"]), None)
            raw["equipment_id"] = cand if cand in eq_ids else None
            notes.append(f"설비 '{e}' → {raw['equipment_id'] or '확인 필요(되묻기)'}")
        z = raw.get("zone_id")
        if z is not None and z not in zone_ids:
            cand = next((x["id"] for x in ctx["zones"] if x["name"] in str(z) or str(z) in x["name"]), None)
            raw["zone_id"] = cand
            notes.append(f"구역 '{z}' → {cand or '확인 필요(되묻기)'}")
        fixed = []
        for w in raw.get("extra_workers") or []:
            wid = worker_id(w)
            if wid:
                fixed.append(wid)
                if wid != w:
                    notes.append(f"작업자 '{w}' → {wid}")
            else:
                notes.append(f"작업자 '{w}': 등록되지 않은 인원이라 제외 (확인 필요)")
        raw["extra_workers"] = fixed
        fw = raw.get("fire_watch")
        if fw is not None:
            if str(fw).strip().lower() in ("없음", "없다", "none", "no", "không", "không có", "미지정"):
                raw["fire_watch"] = "없음"
            else:
                wid = worker_id(fw)
                raw["fire_watch"] = wid
                if wid != fw:
                    notes.append(f"화재감시자 '{fw}' → {wid or '확인 필요(되묻기)'}")
        for k in ("start_hhmm", "end_hhmm"):
            v = raw.get(k)
            if v is not None:
                norm = normalize_hhmm(v)
                if norm != v:
                    notes.append(f"시각 '{v}' → {norm or '확인 필요(되묻기)'}")
                raw[k] = norm
        if raw.get("task") not in L.TASKS:
            notes.append(f"작업 '{raw.get('task')}' → 기타")
            raw["task"] = "기타"
        if raw.get("fire_watch") and raw["fire_watch"] in raw["extra_workers"]:
            raw["extra_workers"] = [w for w in raw["extra_workers"] if w != raw["fire_watch"]]
            notes.append("화재감시자를 공동 작업자 목록에서 제외")
        if raw.get("work_type") not in ("maintenance", "hot_work"):
            raw["work_type"] = "hot_work" if raw["task"] in L.HOT_TASKS else "maintenance"
            notes.append(f"작업 종류 → {raw['work_type']}")
        return raw, notes

    def crosscheck(self, raw, text):
        """신청문 키워드로 LLM의 작업 종류·장소를 교차 확인한다. 키워드가 한 가지로만 가리킬 때만 정정."""
        raw, notes = dict(raw), []
        for w in self.context()["workers"]:            # 사람 이름 속 키워드 오인 방지 (예: '최용접' → 용접, 보류셋 H04)
            text = text.replace(w["name"], " ")
        kw_task = keyword_task(text) or (keyword_task_weak(text) if raw.get("task") == "기타" else None)
        if kw_task and raw.get("task") != kw_task:
            notes.append(f"작업 '{raw.get('task')}' → '{kw_task}' (신청문 키워드)")
            raw["task"] = kw_task
            wt = "hot_work" if kw_task in L.HOT_TASKS else "maintenance"
            if raw.get("work_type") != wt:
                raw["work_type"] = wt
                notes.append(f"작업 종류 → {wt}")
        kw_zone = keyword_zone(text)
        if kw_zone and raw.get("zone_id") != kw_zone:
            notes.append(f"장소 {raw.get('zone_id')} → {kw_zone} (신청문에 명시)")
            raw["zone_id"] = kw_zone
        return raw, notes

    def to_request(self, raw, requester, text, lang, date_str, permit_id):
        """LLM 출력(raw) → 엔진 입력(request). fire_watch: null=모름(키 없음), '없음'=None."""
        raw, notes0 = self.crosscheck(raw, text or "")
        raw, notes = self.sanitize(raw)
        notes = notes0 + notes
        req = {"permit_id": permit_id, "work_type": raw["work_type"], "requester": requester,
               "workers": [requester] + [w for w in raw.get("extra_workers") or [] if w != requester],
               "equipment_id": raw.get("equipment_id"), "zone_id": raw.get("zone_id"), "task": raw["task"],
               "request_text": text, "request_lang": lang}
        if not req["zone_id"] and req["equipment_id"]:
            e = self.con.execute("SELECT zone_id FROM equipment WHERE id=?", (req["equipment_id"],)).fetchone()
            req["zone_id"] = e[0] if e else None
        for k in ("start_hhmm", "end_hhmm"):
            req[k] = raw.get(k)
        for k in ("body_in_danger_zone", "safety_block_used", "die_precheck_completed", "extinguisher_placed",
                  "spark_barrier", "ventilation_done", "excess_combustibles_removed", "post_work_fire_watch_min",
                  "gas_remeasure_plan_4h", "rest_plan_2h_20m", "risk_assessment_done", "claimed_no_combustibles",
                  "claimed_machine_stopped", "emergency"):
            if raw.get(k) is not None:
                req[k] = raw[k]
        fw = raw.get("fire_watch")
        if fw == "없음":
            req["fire_watch"] = None
        elif fw:
            req["fire_watch"] = fw
        req["_date"] = date_str
        req["_notes"] = notes
        if OTHER_DAY.search(text or ""):
            req["_out_of_scope"] = OUT_OF_SCOPE["other_day"]
        return self.finalize_times(req)

    @staticmethod
    def finalize_times(req):
        d = req.get("_date")
        for k, out in (("start_hhmm", "start"), ("end_hhmm", "end")):
            v = normalize_hhmm(req.get(k)) if req.get(k) else None
            req[k] = v
            if v:
                req[out] = f"{d}T{v}"
            else:
                req.pop(out, None)
        return req

    # ── 2. 되묻기 ──
    def pending_questions(self, req):
        lang = req.get("request_lang", "ko")
        out = []
        if req.get("_out_of_scope"):
            return out
        for f in required_fields(req):
            if f == "fire_watch":
                missing = "fire_watch" not in req
            elif f == "crew":
                missing = not req.get("_crew_confirmed")
            else:
                missing = req.get(f) is None
            if missing:
                spec = FIELD_SPECS[f]
                q = spec.get(lang) or spec["ko"]
                out.append({"field": f, "type": spec["type"], "choices": spec.get("choices"),
                            "question": q, "question_ko": spec["ko"],
                            "default": req.get("workers", [])[1:] if f == "crew" else None})
        return out

    def answer(self, req, field_name, value):
        if field_name == "fire_watch":
            req["fire_watch"] = None if value in (None, "없음") else value
        elif field_name == "equipment_id" and value == NO_EQUIPMENT:
            req["equipment_id"] = None
            req["_out_of_scope"] = OUT_OF_SCOPE["no_equipment"]
        elif field_name == "crew":
            valid = {r[0] for r in self.con.execute("SELECT id FROM workers")}
            extra = [w for w in dict.fromkeys(value or []) if w in valid and w != req["requester"]]
            req["workers"] = [req["requester"]] + extra
            req["_crew_confirmed"] = True
        else:
            req[field_name] = value
        return self.finalize_times(req)

    # ── 3. 계획용 사실 ──
    def facts(self, req, now):
        from .tools import Tools
        t = Tools(self.con, now)
        f = {"work_type": req["work_type"], "task": req["task"]}
        if req.get("zone_id"):
            z = t.zone(req["zone_id"])
            f.update(zone=z["name"], location_type=z["location_type"], outdoor=bool(z["outdoor"]),
                     combustibles_within_11m=t.combustibles_within(req["zone_id"], 11))
        if req.get("equipment_id"):
            e = t.equipment(req["equipment_id"])
            f.update(equipment=e["name"], equipment_type=e["type"], stored_energy_types=e["stored_energy_types"],
                     has_electric_isolation=any(x["energy_type"] == "전기" for x in t.isolation_points(e["id"])))
            s = t.latest_sensor(req["equipment_id"])
            if s:
                f["current_a"] = s["current_a"]
        if req.get("start") and req.get("end"):
            f["overlapping_permits"] = [
                {"id": p["id"], "task": p["task"], "zone": p["zone_id"], "handles_combustibles": bool(p["handles_combustibles"])}
                for p in t.permits_overlapping(req["start"], req["end"], req.get("permit_id"))]
        return f

    def plan(self, req, now):
        out, mode = L.plan(self.client, _public(req), self.facts(req, now))
        return out, mode

    # ── 4. 판정 + 모순 강화 + 대안 ──
    def review(self, req, planning, now, weather=None):
        if req.get("_out_of_scope"):
            raise ValueError(req["_out_of_scope"])
        j = self.judge.evaluate(_public(req), planning, now, weather=weather)
        findings, imode = L.inconsistencies(self.client, _public(req), self.facts(req, now))
        adj = [{"name": f"신청 기재 모순: {f['claim']}", "outcome": f["severity"], "origin": f.get("by", "llm"),
                "reason": f"{f['evidence']} (탐지: {'규칙' if f.get('by') == 'rule' else 'LLM'})"} for f in findings]
        if adj:
            self.judge.apply_llm_adjustments(j, adj)
        return j, {"findings": findings, "mode": imode, "alternatives": self.alternatives(j, req)}

    def alternatives(self, j: Judgment, req):
        """반려·조건부 사유별 대안 (결정적)."""
        out = []
        ids = {r.rule_id: r for r in j.active()}
        q = lambda s, *a: [dict(r) for r in self.con.execute(s, a).fetchall()]  # noqa: E731
        busy = {r["worker_id"] for r in q(
            "SELECT pw.worker_id FROM permit_workers pw JOIN permits p ON p.id = pw.permit_id "
            "WHERE p.status IN ('issued','approved') AND p.start_ts < ? AND p.end_ts > ? AND p.id != ?",
            req["end"], req["start"], req["permit_id"])}
        crew = set(req.get("workers", []))
        names = {w["id"]: w["name"] for w in q("SELECT id, name FROM workers")}
        if "H-01" in ids:
            cands = [w for w in q("SELECT id, name, role, can_sign_permit FROM workers")
                     if w["id"] not in crew and w["id"] not in busy and not w["can_sign_permit"]]
            txt = ", ".join(f"{w['name']}({w['role']})" for w in cands) or "가용 인원 없음"
            out.append({"rule": "H-01", "text": f"화재감시자 지정: {txt}  (같은 시간 다른 허가 작업자·허가권자 제외)"})
        for rid, code in (("H-11", "B5-38"), ("M-08", "B5-11")):
            if rid in ids:
                lacking = ids[rid].facts.get("lacking", [])
                who = ", ".join(names.get(w, w) for w in lacking) or "미이수자"
                ws = [w for w in q("SELECT w.id, w.name FROM workers w JOIN trainings t ON t.worker_id=w.id "
                                   "WHERE t.code=?", code) if w["id"] not in crew and w["id"] not in busy]
                if ws:
                    txt = f"별표 5 {code[3:]}호 이수자로 교체 ({', '.join(w['name'] for w in ws)})"
                else:
                    txt = f"교체할 별표 5 {code[3:]}호 이수자 없음(이미 작업 중이거나 같은 시간 다른 허가)"
                out.append({"rule": rid, "text": f"미이수자 {who}: {txt}. 또는 작업에서 빼거나 특별교육 이수 후 재신청"})
        if "X-01" in ids:
            clash = ids["X-01"].facts.get("clash", [])
            if clash:
                ends = [self.con.execute("SELECT end_ts FROM permits WHERE id=?", (c["permit"],)).fetchone()[0]
                        for c in clash]
                shift_end = self.con.execute("SELECT shift_end FROM site").fetchone()[0]
                new_start = datetime.fromisoformat(max(ends))
                dur = datetime.fromisoformat(req["end"]) - datetime.fromisoformat(req["start"])
                new_end = (new_start + dur).strftime("%H:%M")
                t = new_start.strftime("%H:%M")
                if new_end <= shift_end:
                    out.append({"rule": "X-01", "text": f"인접 가연물 취급 작업 종료 후 {t}~{new_end}로 일정 변경"})
                else:
                    out.append({"rule": "X-01", "text": f"인접 작업 종료({t}) 후에는 근무 종료({shift_end}) 전에 "
                                                        f"끝낼 수 없음 → 익일 재신청 (허가 효력 당일 한정, CC49 5.6)"})
        if "M-01" in ids and ids["M-01"].outcome == "reject":
            out.append({"rule": "M-01", "text": "설비 운전 정지 확인 후 재신청"})
        return out

    # ── 5. 증빙 ──
    EVIDENCE = {
        "M-02b": {"kind": "photo_lock", "label": "잠금장치 체결 사진"},
        "H-08": {"kind": "gas", "label": "환기 후 가스 재측정값(LEL %)"},
        "M-04": {"kind": "loto_check", "label": "차단 확인(시동 시도) 기록"},
        "M-05": {"kind": "procedure", "label": "설비별 LOTO 절차서 등록"},
        "M-07": {"kind": "field", "field": "die_precheck_completed", "value": 6, "label": "금형 설치 전 6항목 체크 완료"},
        "C-01": {"kind": "zone", "label": "작업 장소를 설비 소재 구역으로 정정"},
        "C-02": {"kind": "times", "label": "허가기간을 근무시간 이내로 수정"},
        "C-03": {"kind": "field", "field": "risk_assessment_done", "value": True, "label": "작업 전 위험성평가(체크리스트법) 작성·교육 완료"},
        "C-04": {"kind": "field", "field": "rest_plan_2h_20m", "value": True, "label": "매 2시간 20분 휴식 계획 명시"},
        "C-06": {"kind": "times", "label": "14~17시 옥외작업 제외로 시간 수정"},
        "H-06": {"kind": "field", "field": "excess_combustibles_removed", "value": True, "label": "필요량 외 가연물 이동 완료"},
        "H-09": {"kind": "field", "field": "gas_remeasure_plan_4h", "value": True, "label": "4시간 주기 재측정 계획 명시"},
        "H-10": {"kind": "field", "field": "post_work_fire_watch_min", "value": 30, "label": "작업 후 30분 화재감시 계획 명시"},
        "H-13": {"kind": "permit", "label": "정전작업 보충허가 발급"},
    }

    def submit_photo_lock(self, req, point_id, image_bytes, image_name, now):
        """사진 판독 → 자물쇠 확인 시 LOTO 대장 갱신. 불확실하면 사람 확인 대기."""
        pt = dict(self.con.execute("SELECT * FROM isolation_points WHERE id=?", (point_id,)).fetchone())
        vis, vmode = L.read_lock_photo(self.client, image_bytes, f"{pt['equipment_id']} {pt['description']}")
        ok = bool(vis and vis["padlock_present"])
        summary = (f"자물쇠 {'있음' if vis['padlock_present'] else '없음'}, 꼬리표 {vis.get('tag_name') or '-'}: "
                   f"{vis['observation']}") if vis else "VLM 미연결: 사람 확인 필요"
        eid = self.judge.submit_evidence(req["permit_id"], "M-02b", now, image_name, vlm_result=summary)
        if ok:
            self._lock(point_id, now)
        return {"evidence_id": eid, "vlm": vis, "mode": vmode, "registry_updated": ok, "summary": summary}

    def confirm_photo(self, evidence_id, point_id, confirmed, now, by):
        self.con.execute("UPDATE evidence SET human_confirmed=? WHERE id=?", (1 if confirmed else 0, evidence_id))
        if confirmed:
            self._lock(point_id, now)
        self.con.commit()

    def _lock(self, point_id, now):
        self.con.execute("UPDATE isolation_points SET locked=1, updated_at=? WHERE id=?", (now, point_id))
        self.con.commit()

    def submit_gas(self, req, lel, o2, now, by):
        self.con.execute("INSERT INTO gas_readings (zone_id, ts, lel_percent, o2, measured_by) VALUES (?,?,?,?,?)",
                         (req["zone_id"], now, lel, o2, by))
        self.judge.submit_evidence(req["permit_id"], "H-08", now, value={"lel_percent": lel, "o2": o2})

    def submit_loto_check(self, req, method, now, by):
        self.con.execute("INSERT INTO loto_checks (equipment_id, stored_energy_released, isolation_verified, "
                         "verify_method, checked_by, checked_at) VALUES (?,1,1,?,?,?)",
                         (req["equipment_id"], method, by, now))
        self.judge.submit_evidence(req["permit_id"], "M-04", now, value={"method": method})

    def submit_procedure(self, req, title, now):
        self.con.execute("INSERT OR REPLACE INTO procedures VALUES (?,?,?,?,?,?)",
                         (f"PR-LOTO-{req['equipment_id']}", req["equipment_id"], "LOTO", None, title, now[:10]))
        self.judge.submit_evidence(req["permit_id"], "M-05", now, value={"title": title})

    def submit_field(self, req, rule_id, now):
        spec = self.EVIDENCE[rule_id]
        req[spec["field"]] = spec["value"]
        self.judge.submit_evidence(req["permit_id"], rule_id, now, value={spec["field"]: spec["value"]})
        return req

    def submit_supplementary_permit(self, req, now):
        pid = f"{req['permit_id']}-PW"
        self.con.execute("INSERT OR REPLACE INTO permits (id, type, requester_id, equipment_id, zone_id, task, start_ts, "
                         "end_ts, status) VALUES (?,?,?,?,?,?,?,?,'issued')",
                         (pid, "정전작업", req["requester"], req.get("equipment_id"), req["zone_id"], "정전",
                          req["start"], req["end"]))
        self.judge.submit_evidence(req["permit_id"], "H-13", now, value={"permit": pid})

    # ── 6. 문서 ──
    def documents(self, j: Judgment, req, final_status=None, resolutions=None):
        status = final_status or j.status
        resolutions = resolutions or {}

        def shown(r):
            v = resolutions.get(r.rule_id)
            if r.outcome == "human_review" and v:
                return f"사람 판단 → {'수용' if v['decision'] == 'accept' else '불수용'}" + (f" ({v['note']})" if v.get("note") else "")
            return OUTCOME_KO.get(r.outcome, r.outcome)
        w = {r["id"]: r["name"] for r in self.con.execute("SELECT id, name FROM workers")}
        z = self.con.execute("SELECT name FROM zones WHERE id=?", (req["zone_id"],)).fetchone()[0]
        eq = req.get("equipment_id") or "-"
        form = "별지 양식 1 화기작업허가서" if req["work_type"] == "hot_work" else "별지 양식 2 일반위험작업 허가서"
        crew = ", ".join(w.get(x, x) for x in req.get("workers", []))
        fw = w.get(req.get("fire_watch"), "미지정") if req["work_type"] == "hot_work" else "-"
        checks = [r for r in j.results if r.outcome != "n/a" and r.origin == "rule"]
        lines = [f"# 작업허가서 초안 ({form} 구조 준용, KOSHA GUIDE C-C-49-2026)",
                 "", f"- 허가번호: {j.permit_id}", f"- 상태: {STATUS_KO.get(status, status)}",
                 f"- 작업: {req['task']} / 설비 {eq} / 장소 {z}",
                 f"- 일시: {req.get('start', '').replace('T', ' ')} ~ {req.get('end', '')[11:]} (효력: 당일 정상근무시간 이내, C-C-49 5.6)",
                 f"- 작업자: {crew} / 화재감시자: {fw}", "", "## 안전조치 확인 결과", "",
                 "| 규칙 | 항목 | 결과 | 근거 |", "|---|---|---|---|"]
        for r in checks:
            cl = r.clause if isinstance(r.clause, str) else (", ".join(r.clause) if r.clause else "")
            lines.append(f"| {r.rule_id} | {r.name} | {shown(r)} | {r.source_id} {cl} ({r.grade}) |")
        extra = [r for r in j.results if r.origin in ("llm", "system", "planning")]
        if extra:
            lines += ["", "## 추가 검토 사항 (설계 등급: 법적 판정 아님)"]
            lines += [f"- [{shown(r)}] {r.name}: {r.reason}" for r in extra]
        lines += ["", "## 서명", f"- 발급자(신청): {w.get(req['requester'])}",
                  "- 허가권자: ________ (서명 전까지 효력 없음)"]
        permit_md = "\n".join(lines)

        posting = "\n".join([
            "【화재위험작업 게시】 (산업안전보건기준에 관한 규칙 제241조④)" if req["work_type"] == "hot_work"
            else "【정비작업 게시】 (운전정지·잠금 중, 제92조)",
            f"작업내용: {req['task']} ({eq})", f"작업일시: {req.get('start', '').replace('T', ' ')} ~ {req.get('end', '')[11:]}",
            f"장소: {z}", f"작업자: {crew}" + (f" / 화재감시자: {fw}" if req["work_type"] == "hot_work" else ""),
            "안전조치: " + ", ".join(r.name for r in checks if r.outcome == "pass")[:300]])

        notice_ko = self.worker_notice_ko(j, req, status)
        return {"permit_md": permit_md, "posting": posting, "notice_ko": notice_ko}

    def worker_notice_ko(self, j, req, status):
        head = {"issued": "작업이 허가되었습니다.", "approved": "검토를 통과했습니다. 허가권자 서명을 기다립니다.",
                "conditional": "아래 조치를 하면 다시 확인합니다.", "human_review": "안전관리자가 확인 중입니다.",
                "rejected": "이 작업은 지금 할 수 없습니다.", "suspended": "작업을 즉시 멈추십시오."}.get(status, status)
        parts = [head]
        for r in j.active():
            if r.outcome == "reject":
                cl = r.clause if isinstance(r.clause, str) else ""
                parts.append(f"- 이유: {r.name} ({r.source_title.split('(')[0].strip()} {cl})".replace(" )", ")"))
            elif r.outcome == "conditional":
                parts += [f"- 해야 할 일: {c}" for c in r.conditions]
        return "\n".join(parts)

    def translate(self, text_ko, lang):
        return L.translate(self.client, text_ko, lang)

    # ── 7. 작업 감시 ──
    def monitor(self, permit_id, now):
        """발급 후 감시. 정지 사유가 있으면 status='suspended'."""
        from .tools import Tools
        p = dict(self.con.execute("SELECT * FROM permits WHERE id=?", (permit_id,)).fetchone())
        req = json.loads(p["request_json"])
        t = Tools(self.con, now)
        alerts = []
        if req["work_type"] == "maintenance" and req.get("equipment_id"):
            s = t.latest_sensor(req["equipment_id"])
            if s is None or s.get("stale"):
                last = s["ts"][11:16] if s else "없음"
                alerts.append({"level": "warn", "text": f"설비 센서 데이터 끊김 (마지막 수신 {last}): 가동 여부를 현장에서 확인",
                               "source": "DP-03 (설계), SP-02"})
            elif s["current_a"] > 0:
                alerts.append({"level": "stop", "text": f"설비 가동 감지 (전류 {s['current_a']}A): 정비 중 운전 금지", "source": "SR-92 ①"})
            open_pts = [x["id"] for x in t.isolation_points(req["equipment_id"]) if x["lockable"] and not x["locked"]]
            if open_pts:
                alerts.append({"level": "stop", "text": f"잠금 해제 감지: {', '.join(open_pts)}", "source": "SR-92 ②, BM25 5.2"})
        if req["work_type"] == "hot_work":
            g = t.latest_gas(req["zone_id"])
            if g is None:
                alerts.append({"level": "warn", "text": "오늘 이 구역의 가스 측정값 없음: 측정 필요", "source": "P35 5.2(1), DP-03"})
            if g and g["lel_percent"] is not None and g["lel_percent"] >= 10:
                alerts.append({"level": "stop", "text": f"가스농도 LEL {g['lel_percent']}%: 화기작업 중단", "source": "P35 5.2(1) (KOSHA권고)"})
            if g and datetime.fromisoformat(now) - datetime.fromisoformat(g["ts"]) > timedelta(hours=4):
                alerts.append({"level": "warn", "text": "마지막 가스 측정 후 4시간 경과: 재측정 필요", "source": "P35 5.2(1)"})
            end = datetime.fromisoformat(p["end_ts"])
            if datetime.fromisoformat(now) >= end:
                left = end + timedelta(minutes=req.get("post_work_fire_watch_min") or 30) - datetime.fromisoformat(now)
                if left.total_seconds() > 0:
                    alerts.append({"level": "info", "text": f"작업 후 화재감시 중: {int(left.total_seconds() // 60)}분 남음", "source": "AG14 7.1(17)"})
        if any(a["level"] == "stop" for a in alerts) and p["status"] == "issued":
            self.con.execute("UPDATE permits SET status='suspended' WHERE id=?", (permit_id,))
            self.judge._log(permit_id, now, "감시", None, None, None, "suspended", "; ".join(a["text"] for a in alerts))
            self.con.commit()
        return alerts, t.trace


TASK_KEYWORDS = [   # (정규식, 작업) — 순서 무관, 두 가지 이상 걸리면 정정하지 않음
    (r"용접|hàn|weld", "용접"), (r"용단|절단|cắt|cutting|torch", "용단"), (r"연마|그라인더|mài|grind", "연마"),
    (r"드릴|천공|khoan|drill", "드릴"), (r"금형\s*(교체|바꾸|바꿉|교환)", "금형교체"), (r"금형\s*조정", "금형조정"),
    (r"금형\s*(부착|설치)", "금형부착"), (r"금형\s*(해체|탈거)", "금형해체"), (r"수리|고치|고쳐|sửa|repair", "수리"),
    (r"청소|vệ sinh|clean", "청소"),
]
ZONE_KEYWORDS = [
    (r"2\s*라인|line\s*2|dây chuyền 2", "W2"), (r"도장\s*부스|paint booth", "PB"),
    (r"용접\s*전용\s*부스|전용\s*부스|buồng hàn|welding booth", "W1"), (r"1\s*라인|line\s*1|dây chuyền 1", "L1"),
    (r"자재\s*창고|warehouse", "MS"), (r"옥외|야외|적치장|outdoor|yard", "YD"),
]


WEAK_TASK_KEYWORDS = [(r"점검|확인|검사|윤활|그리스|주유|kiểm tra|inspect|check|lubric|grease", "점검")]


def keyword_task_weak(text):
    """약한 키워드: LLM이 '기타'라고 답했을 때만 쓴다 ('확인' 같은 말은 다른 뜻으로도 흔함)."""
    hits = {t for pat, t in WEAK_TASK_KEYWORDS if re.search(pat, text, re.I)}
    return hits.pop() if len(hits) == 1 else None


def keyword_task(text):
    hits = {t for pat, t in TASK_KEYWORDS if re.search(pat, text, re.I)}
    if {"금형교체", "금형조정", "금형부착", "금형해체"} & hits:
        hits -= {"수리"}
    return hits.pop() if len(hits) == 1 else None


def keyword_zone(text):
    hits = {z for pat, z in ZONE_KEYWORDS if re.search(pat, text, re.I)}
    if "W1" in hits:
        hits -= {"W2"} if not re.search(r"2\s*라인|line\s*2|dây chuyền 2", text, re.I) else set()
    return hits.pop() if len(hits) == 1 else None


def normalize_hhmm(v):
    """'9:00'·'14시'·'14시 30분'·'14h30' → 'HH:MM'. 해석할 수 없으면 None (되묻기)."""
    if v is None:
        return None
    t = str(v).strip()
    m = re.fullmatch(r"(오전|오후|AM|PM|am|pm)?\s*([01]?\d|2[0-3])\s*(?::|시|h)\s*([0-5]\d)?\s*분?(?::[0-5]\d)?", t)
    if not m:
        return None
    h = int(m.group(2))
    if m.group(1) in ("오후", "PM", "pm") and h < 12:
        h += 12
    elif m.group(1) in ("오전", "AM", "am") and h == 12:
        h = 0
    return f"{h:02d}:{m.group(3) or '00'}"


def _public(req):
    """엔진에 넘길 때 내부 키(_date 등)는 뺀다."""
    return {k: v for k, v in req.items() if not k.startswith("_") and k not in ("start_hhmm", "end_hhmm")}


def distance(a, b):
    return math.hypot(a["x_m"] - b["x_m"], a["y_m"] - b["y_m"])
