"""로컬 LLM 계층 (Ollama + Gemma 4).

LLM이 맡는 일 (판정은 하지 않는다)
1. structure  : 자유 형식 신청 → 구조화 필드. 명시되지 않은 항목은 null (되묻기 대상)
2. plan       : C-C-49 6.1 14항목 해당/비해당/모름 + 이유
3. inconsist  : 신청 기재와 도구 조회 결과의 모순 탐지 → 강화 조정 제안 (엔진이 human_review로 상한 제한)
4. translate  : 안내문을 작업자 언어로 번역
5. vision     : 증빙 사진 판독 (자물쇠 체결 여부 등)

LLM이 연결되지 않으면 각 함수는 결정적 폴백을 쓰고 mode='fallback'을 돌려준다 (LIMITATIONS.md L-C06).
LLM 호출이 실패(시간 초과·연결 끊김·JSON 오류)해도 예외를 올리지 않고 폴백으로 내려간다 (mode='fallback:error').
"""
import base64
import io
import json
import re
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemma4:31b"
DEFAULT_HOST = "http://localhost:11434"
KEEP_ALIVE = "30m"      # 설계값: 시연 중 모델이 내려가 첫 호출이 17초 걸리는 것 방지 (L-C07)
NUM_CTX = 8192          # 설계값: 프롬프트는 3천 토큰 이하. 최대 26만 토큰 컨텍스트를 잡지 않게 제한
MAX_IMAGE_PX = 1024     # 설계값: 증빙 사진 긴 변을 줄여 판독 지연 감소
LLM_ERRORS = (urllib.error.URLError, OSError, TimeoutError, ValueError, KeyError, TypeError)

TASKS = ["점검", "수리", "청소", "금형교체", "금형부착", "금형해체", "금형조정", "용접", "용단", "연마", "드릴", "기타"]
HOT_TASKS = {"용접", "용단", "연마", "드릴"}
SPARK_BARRIER_CHOICES = ["인증방화포", "비산방지덮개", "일반 방화포(인증 미확인)", "없음"]


class OllamaClient:
    def __init__(self, model=DEFAULT_MODEL, host=DEFAULT_HOST, timeout=300):
        self.model, self.host, self.timeout = model, host.rstrip("/"), timeout
        self.calls = []            # 호출 기록 (실행 로그·보고서용)

    def available(self):
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=3) as r:
                names = [m["name"] for m in json.load(r)["models"]]
            return self.model in names
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            return False

    def warmup(self):
        """모델을 GPU에 미리 올려 둔다 (프롬프트 없는 generate 호출). 실패해도 조용히 넘어간다."""
        try:
            body = {"model": self.model, "keep_alive": KEEP_ALIVE, "options": {"num_ctx": NUM_CTX}}
            req = urllib.request.Request(f"{self.host}/api/generate", json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                r.read()
            return True
        except LLM_ERRORS:
            return False

    def chat(self, system, user, schema, images=None, purpose=""):
        msg = {"role": "user", "content": user}
        if images:
            msg["images"] = [base64.b64encode(b).decode() for b in images]
        body = {"model": self.model, "stream": False, "think": False, "format": schema, "keep_alive": KEEP_ALIVE,
                "options": {"temperature": 0, "num_ctx": NUM_CTX},
                "messages": [{"role": "system", "content": system}, msg]}
        req = urllib.request.Request(f"{self.host}/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                d = json.load(r)
            out = json.loads(d["message"]["content"])
        except LLM_ERRORS as e:
            self.calls.append({"purpose": purpose, "seconds": None, "error": f"{type(e).__name__}: {e}"})
            raise
        self.calls.append({"purpose": purpose, "seconds": round(d.get("total_duration", 0) / 1e9, 1),
                           "output": out})
        return out


def _shrink_image(data, max_px=MAX_IMAGE_PX):
    """긴 변이 max_px를 넘으면 줄여서 PNG로 다시 인코딩. Pillow가 없거나 실패하면 원본."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if max(im.size) <= max_px:
            return data
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return data


# ─────────────────────────────────────────────
# 1. 신청 구조화
# ─────────────────────────────────────────────
NB = {"type": ["boolean", "null"]}
STRUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "work_type": {"type": "string", "enum": ["maintenance", "hot_work"]},
        "equipment_id": {"type": ["string", "null"]},
        "zone_id": {"type": ["string", "null"]},
        "task": {"type": "string", "enum": TASKS},
        "start_hhmm": {"type": ["string", "null"]},
        "end_hhmm": {"type": ["string", "null"]},
        "extra_workers": {"type": "array", "items": {"type": "string"}},
        "fire_watch": {"type": ["string", "null"]},        # 작업자 ID | "없음" | null(언급 없음)
        "body_in_danger_zone": NB, "safety_block_used": NB,
        "die_precheck_completed": {"type": ["integer", "null"]},
        "extinguisher_placed": NB,
        "spark_barrier": {"type": ["string", "null"], "enum": SPARK_BARRIER_CHOICES + [None]},
        "ventilation_done": NB, "excess_combustibles_removed": NB,
        "post_work_fire_watch_min": {"type": ["integer", "null"]},
        "gas_remeasure_plan_4h": NB, "rest_plan_2h_20m": NB, "risk_assessment_done": NB,
        "claimed_no_combustibles": NB, "claimed_machine_stopped": NB, "emergency": NB,
        "summary_ko": {"type": "string"},
    },
    "required": ["work_type", "equipment_id", "zone_id", "task", "start_hhmm", "end_hhmm", "extra_workers",
                 "fire_watch", "body_in_danger_zone", "safety_block_used", "die_precheck_completed",
                 "extinguisher_placed", "spark_barrier", "ventilation_done", "excess_combustibles_removed",
                 "post_work_fire_watch_min", "gas_remeasure_plan_4h", "rest_plan_2h_20m", "risk_assessment_done",
                 "claimed_no_combustibles", "claimed_machine_stopped", "emergency", "summary_ko"],
}

STRUCT_SYSTEM = """너는 제조 현장 작업허가 신청을 구조화하는 보조자다. 판정은 하지 않는다.
규칙:
- 신청문에 명시적으로 적힌 것만 채운다. 적히지 않은 항목은 반드시 null이다. 추측하지 않는다.
- 화기작업(용접·용단·절단·연마·드릴·그라인더)은 work_type=hot_work, 그 외 정비·점검·수리·금형 작업은 maintenance.
- equipment_id와 zone_id는 제공된 목록의 ID만 쓴다. 해당이 없으면 null.
- fire_watch: 화재감시자로 지정된 작업자 ID. '없다'고 명시하면 "없음". 언급이 없으면 null.
- extra_workers: 신청자와 **함께 작업하는** 사람만. 화재감시자·입회자는 넣지 않는다.
- 시각은 24시간 HH:MM. '오전 중'처럼 모호하면 null.
- claimed_no_combustibles: 신청자가 '주변에 가연물이 없다'고 주장할 때만 true.
  '불필요한 가연물을 치웠다/정리했다'는 claimed_no_combustibles가 아니라 excess_combustibles_removed=true다.
- claimed_machine_stopped: 신청자가 '설비를 정지했다'고 주장하면 true.
- summary_ko: 신청 내용을 한국어 한 문장으로 요약."""


def _context_text(ctx):
    eq = "; ".join(f"{e['id']}={e['name']}({e['zone_id']})" for e in ctx["equipment"])
    zn = "; ".join(f"{z['id']}={z['name']}" for z in ctx["zones"])
    wk = "; ".join(f"{w['id']}={w['name']}({w['role']})" for w in ctx["workers"])
    return f"설비: {eq}\n구역: {zn}\n작업자: {wk}"


def structure(client, text, requester, ctx):
    if client:
        user = f"{_context_text(ctx)}\n신청자: {requester}\n신청문: {text}"
        try:
            return client.chat(STRUCT_SYSTEM, user, STRUCT_SCHEMA, purpose="신청 구조화"), "llm"
        except LLM_ERRORS:
            return structure_fallback(text, ctx), "fallback:error"
    return structure_fallback(text, ctx), "fallback"


def structure_fallback(text, ctx):
    """LLM 미연결 시 키워드·정규식 구조화. 한국어 정형 문장만 안정적으로 처리한다 (L-C06)."""
    out = {k: None for k in STRUCT_SCHEMA["properties"]}
    out.update(extra_workers=[], summary_ko=text[:80])
    for w in ctx["workers"]:
        if w["name"] not in text:
            continue
        if re.search(rf"(감시자?|giám sát cháy|fire\s*watch)[^.,\n]{{0,8}}{re.escape(w['name'])}", text, re.I):
            out["fire_watch"] = w["id"]
        else:
            out["extra_workers"].append(w["id"])
    for w in ctx["workers"]:                     # 이름 속 키워드 오인 방지 (예: '최용접')
        text = text.replace(w["name"], " ")
    hot = re.search(r"용접|용단|절단|연마|그라인더|드릴|hàn|cắt|mài|weld|cutting|grind|torch", text, re.I)
    out["work_type"] = "hot_work" if hot else "maintenance"
    m = re.search(r"(\d)\s*호기", text)
    if m:
        eid = f"P-0{m.group(1)}"
        if any(e["id"] == eid for e in ctx["equipment"]):
            out["equipment_id"] = eid
            out["zone_id"] = next(e["zone_id"] for e in ctx["equipment"] if e["id"] == eid)
    if re.search(r"2\s*라인|dây chuyền 2|line\s*2", text, re.I):
        out["zone_id"] = out["zone_id"] or "W2"
        if hot:
            out["equipment_id"] = "WD-01"
    task = "기타"
    for kw, t in [("금형", "금형교체"), ("용접|hàn|weld", "용접"), ("용단|절단|cắt|cutting", "용단"),
                  ("연마|그라인더|mài|grind", "연마"),
                  ("수리", "수리"), ("점검|봐야|소리", "점검"), ("청소", "청소")]:
        if re.search(kw, text, re.I):
            task = t
            break
    out["task"] = task
    times = re.findall(r"(\d{1,2})\s*(?:시|h|:00)", text)
    if len(times) >= 2:
        out["start_hhmm"], out["end_hhmm"] = f"{int(times[0]):02d}:00", f"{int(times[1]):02d}:00"
    if re.search(r"가연물.{0,4}없|không có vật dễ cháy", text):
        out["claimed_no_combustibles"] = True
    if re.search(r"정지(했|시켰)", text):
        out["claimed_machine_stopped"] = True
    return out


# ─────────────────────────────────────────────
# 2. 14항목 계획
# ─────────────────────────────────────────────
PLAN_ITEMS = {
    1: "밀폐공간 내 작업 여부", 2: "안전상 정전(전기차단) 필요 여부", 3: "굴착작업 병행 여부",
    4: "방사선사용 작업 여부", 5: "위험지역 대신 안전한 장소에서 작업 가능 여부",
    6: "인화성·독성물질 발생 가능성, 처리·세정방법 적정성", 7: "잠긴 밸브·막힌 배관 사이 액체 열팽창 가능성",
    8: "내부구조상 유해·위험물질 잔류 가능성", 9: "산소·유해가스 농도 측정 및 강제환기 필요성",
    10: "초기 소화설비 배치계획", 11: "출입 제한구역 계획", 12: "작업 중 현장 입회자 필요 여부",
    13: "고소작업 사고예방대책", 14: "중장비 작업 사고예방대책",
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {str(i): {"type": "object", "properties": {
        "answer": {"type": "string", "enum": ["해당", "비해당", "모름"]}, "reason": {"type": "string"}},
        "required": ["answer", "reason"]} for i in PLAN_ITEMS},
    "required": [str(i) for i in PLAN_ITEMS],
}
PLAN_SYSTEM = """너는 KOSHA GUIDE C-C-49-2026 6.1 '작업허가 전 점검사항' 14항목을 판단하는 보조자다.
각 항목에 해당/비해당/모름과 근거를 한 줄로 적는다. 근거는 제공된 사실에서만 가져온다.
제공된 사실로 판단할 수 없으면 '모름'이다 (모름은 사람 확인으로 넘어간다). 판정·허가 여부는 말하지 않는다."""


def plan(client, request, facts):
    """하이브리드: 데이터로 정해지는 항목은 규칙이 확정하고, LLM은 '모름' 항목만 채운다 (덮어쓰기 금지)."""
    base = plan_data(request, facts)
    unknown = [k for k, v in base.items() if v["answer"] == "모름"]
    if not client or not unknown:
        return base, "fallback" if not client else "data"
    fixed = {k: v for k, v in base.items() if k not in unknown}
    items = "\n".join(f"{k}. {PLAN_ITEMS[int(k)]}" for k in unknown)
    user = (f"판단할 항목:\n{items}\n\n이미 데이터로 확정된 항목(수정 금지): {json.dumps(fixed, ensure_ascii=False)}\n"
            f"신청(구조화): {json.dumps(request, ensure_ascii=False)}\n사실: {json.dumps(facts, ensure_ascii=False)}")
    schema = {"type": "object", "properties": {k: PLAN_SCHEMA["properties"][k] for k in unknown}, "required": unknown}
    try:
        out = client.chat(PLAN_SYSTEM, user, schema, purpose="14항목 계획(미확정 항목)")
    except LLM_ERRORS:
        return base, "fallback:error"
    for k in unknown:
        if k in out:
            reason = re.sub(r"^(\s*\[[^\]]{1,6}\]\s*)+", "", out[k]["reason"])   # LLM이 붙이는 [데이터] 등 태그 제거
            base[k] = {"answer": out[k]["answer"], "reason": f"[LLM] {reason}"}
    return base, "hybrid"


def plan_data(request, facts):
    """데이터로 판단 가능한 항목만 확정하고 나머지는 '모름'."""
    hot = request["work_type"] == "hot_work"
    near = facts.get("combustibles_within_11m") or []
    loc = facts.get("location_type", "일반")
    stored = facts.get("stored_energy_types") or ""
    elec = facts.get("has_electric_isolation", False)
    ans = {}

    def put(i, a, r):
        ans[str(i)] = {"answer": a, "reason": f"[데이터] {r}" if a != "모름" else f"[미확정] {r}"}

    put(1, "비해당" if loc == "일반" else "모름", f"구역 유형 '{loc}'")
    if hot:
        put(2, "모름", "화기작업 중 전기설비 정비 병행 여부는 신청 정보로 알 수 없음")
    else:
        put(2, "해당" if elec else "비해당", f"설비 전기 차단점 {'있음' if elec else '없음'}")
    put(3, "해당" if loc == "가스도관굴착" else "비해당", f"구역 유형 '{loc}'")
    put(4, "모름", "방사선 검사 병행 여부")
    put(5, "모름", "대체 장소 가능 여부는 현장 판단")
    if hot and near:
        put(6, "해당", "11m 이내 가연물: " + ", ".join(f"{c['name']} {c['distance_m']}m" for c in near))
    else:
        put(6, "모름", "인화성·독성물질 발생 가능성")
    has_fluid = any(x in stored for x in ("유압", "공압"))
    put(7, "해당" if has_fluid else "비해당", f"잔류에너지 '{stored or '없음'}'")
    put(8, "해당" if has_fluid else "비해당", f"잔류에너지 '{stored or '없음'}'")
    put(9, "해당" if hot else "모름", "화기작업 가스농도 측정 (C-C-49 7.1(3)(나))" if hot else "가스 측정 필요성")
    put(10, "해당" if hot else "모름", "화기작업 소화설비 (C-C-49 7.1(3))" if hot else "소화설비 필요성")
    put(11, "해당" if hot else "모름", "화기작업 작업구역 설정 (C-C-49 7.1(3)(가))" if hot else "출입 제한 필요성")
    put(12, "해당" if (hot and near) else "모름", "11m 이내 가연물 → 화재감시자 (제241조의2)" if (hot and near) else "입회자 필요성")
    put(13, "모름", "고소작업 여부")
    put(14, "모름", "중장비 사용 여부")
    return ans


# ─────────────────────────────────────────────
# 3. 모순 탐지 (강화 조정 제안)
# ─────────────────────────────────────────────
INCONS_SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {"type": "object", "properties": {
        "claim": {"type": "string"}, "evidence": {"type": "string"}, "severity": {"type": "string", "enum": ["warn", "human_review"]},
        "related_rule": {"type": ["string", "null"]}}, "required": ["claim", "evidence", "severity", "related_rule"]}}},
    "required": ["findings"],
}
INCONS_SYSTEM = """너는 작업허가 신청서의 기재 내용과 시스템 조회 결과(사실)를 대조해 모순을 찾는 보조자다.
- 모순은 '신청서 주장'과 '조회된 사실'이 서로 다른 경우만 적는다. 근거로 사실의 값을 인용한다.
- 모순이 없으면 findings는 빈 배열이다. 새로운 위험을 지어내지 않는다.
- severity는 warn 또는 human_review만 쓴다. 반려·승인은 네가 결정하지 않는다."""


def inconsistencies(client, request, facts):
    found = inconsistencies_rules(request, facts)
    if client:
        known = [f"{f['claim']} ↔ {f['evidence']}" for f in found]
        user = (f"신청서: {json.dumps(request, ensure_ascii=False)}\n조회 사실: {json.dumps(facts, ensure_ascii=False)}\n"
                f"이미 탐지된 모순(같은 내용을 다시 적지 말 것): {json.dumps(known, ensure_ascii=False)}")
        try:
            out = client.chat(INCONS_SYSTEM, user, INCONS_SCHEMA, purpose="모순 탐지")
        except LLM_ERRORS:
            return found, "fallback:error"
        found += [dict(f, by="llm") for f in out["findings"]]
        return found, "llm"
    return found, "fallback"


def inconsistencies_rules(request, facts):
    """결정적 대조: 신청 주장 필드 vs 도구 결과 (LLM 없이도 항상 실행)."""
    out = []
    near = facts.get("combustibles_within_11m") or []
    if request.get("claimed_no_combustibles") and near:
        ev = ", ".join(f"{c['name']} {c['distance_m']}m({c['types']})" for c in near)
        out.append({"claim": "신청서 '주변에 가연물 없음'", "evidence": f"배치도상 11m 이내 가연물: {ev}",
                    "severity": "human_review", "related_rule": "H-06", "by": "rule"})
    if request.get("claimed_machine_stopped") and (facts.get("current_a") or 0) > 0:
        out.append({"claim": "신청서 '설비 정지함'", "evidence": f"최신 전류 {facts['current_a']}A",
                    "severity": "human_review", "related_rule": "M-01", "by": "rule"})
    return out


# ─────────────────────────────────────────────
# 4. 번역
# ─────────────────────────────────────────────
LANG_NAME = {"ko": "한국어", "vi": "베트남어", "en": "영어", "ne": "네팔어", "km": "크메르어", "id": "인도네시아어"}
TR_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


def translate(client, text_ko, lang):
    if lang == "ko":
        return text_ko, "none"
    if client:
        sys_ = (f"다음 한국어 산업안전 안내문을 {LANG_NAME.get(lang, lang)}로 번역한다. 쉬운 문장으로. "
                "조항 번호, 설비 ID, 시각, 수치는 그대로 둔다. 내용을 더하거나 빼지 않는다.")
        try:
            return client.chat(sys_, text_ko, TR_SCHEMA, purpose=f"번역({lang})")["text"], "llm"
        except LLM_ERRORS:
            return None, "fallback:error"
    return None, "fallback"


# ─────────────────────────────────────────────
# 5. 증빙 사진 판독
# ─────────────────────────────────────────────
VISION_SCHEMA = {"type": "object", "properties": {
    "padlock_present": {"type": "boolean"}, "tag_present": {"type": "boolean"},
    "tag_name": {"type": ["string", "null"]}, "observation": {"type": "string"}},
    "required": ["padlock_present", "tag_present", "tag_name", "observation"]}
VISION_SYSTEM = ("너는 LOTO(잠금·표지) 증빙 사진 판독 보조다. 보이는 것만 말한다. "
                 "꼬리표(태그)만 있는 것은 자물쇠가 아니다. 판정·허가는 하지 않는다.")


def read_lock_photo(client, image_bytes, point_desc):
    if not client:
        return None, "fallback"
    user = f"대상: {point_desc}. 이 차단장치에 자물쇠가 물리적으로 체결되어 있는가? 꼬리표 이름은?"
    try:
        return client.chat(VISION_SYSTEM, user, VISION_SCHEMA, images=[_shrink_image(image_bytes)],
                           purpose="증빙 사진 판독"), "llm"
    except LLM_ERRORS:
        return None, "fallback:error"
