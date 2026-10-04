"""AI 안전작업허가 검토관 GUI.  실행: streamlit run app.py

흐름: ① 신청(구조화·되묻기·14항목 계획) → ② 검토·조치(판정·모순·대안·증빙·재검증) → ③ 서명·발행 → ④ 작업 감시
"""
import json
import re
import sqlite3
import sys
import threading
from datetime import date, time
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "data"))
import seed  # noqa: E402

from engine import llm as L  # noqa: E402
from engine import pipeline as P  # noqa: E402
from engine import scenarios as sc  # noqa: E402
from engine.pipeline import FIELD_LABEL_KO, Pipeline  # noqa: E402
from engine.rules import RuleBook  # noqa: E402

DB = ROOT / "data" / "permit_demo.db"
EVID_DIR = ROOT / "data" / "evidence"
SAMPLES = ROOT / "assets" / "samples"
LIMITS = ROOT / "LIMITATIONS.md"
PAGES = ["① 신청", "② 검토·조치", "③ 서명·발행", "④ 작업 감시", "⑤ 기록", "⑥ 규칙·한계"]
ICON = {"reject": "🟥 반려", "conditional": "🟧 조건부", "human_review": "🟦 사람 판단", "warn": "🟨 경고",
        "pass": "✅ 통과", "n/a": "· 비적용"}
STATUS_UI = {"approved": ("success", "승인: 허가권자 서명 대기"), "conditional": ("warning", "조건부 승인: 조치 후 재검증"),
             "human_review": ("info", "사람 판단 필요: 허가권자가 결정"), "rejected": ("error", "반려"),
             "issued": ("success", "허가서 발행"), "suspended": ("error", "작업 정지")}
LANGS = {"ko": "한국어", "vi": "Tiếng Việt", "en": "English", "ne": "नेपाली", "km": "ខ្មែរ", "id": "Bahasa Indonesia"}

st.set_page_config(page_title="AI 안전작업허가 검토관", page_icon="🦺", layout="wide")


def md(text):
    """Streamlit 마크다운은 한 줄의 ~...~를 취소선으로 그린다. 범위 표기(14~17시 등)가 깨지지 않게 이스케이프."""
    return re.sub(r"(?<!\\)~", r"\\~", text)


ss = st.session_state


# ─────────────────────────────────────────────
# 상태
# ─────────────────────────────────────────────
def reset_db():
    # 파일을 지우지 않고 새 시드 DB를 내용째 복사한다. 다른 브라우저 세션이 연결을 잡고 있어도
    # Windows 파일 잠금으로 실패하지 않는다.
    tmp = DB.with_name("permit_demo.seed.db")
    seed.build(tmp, date.fromisoformat(sc.DEMO_DATE))
    src, dst = sqlite3.connect(tmp), sqlite3.connect(DB)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
        tmp.unlink(missing_ok=True)
    if "con" in ss:
        ss.con.close()
    for k in ("con", "pl", "raw", "req", "planning", "j", "extra", "final", "docs", "photo", "round", "notice_tr"):
        ss.pop(k, None)


def db_version():
    c = None
    try:
        c = sqlite3.connect(DB)          # with 문은 닫지 않으므로 직접 close (Windows 파일 잠금)
        return c.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error:
        return -1
    finally:
        if c:
            c.close()


if not DB.exists() or db_version() != seed.SCHEMA_VERSION:
    reset_db()
ss.setdefault("page", PAGES[0])
ss.setdefault("clock", time(10, 30))
ss.setdefault("text", "")
ss.setdefault("requester", "W001")
ss.setdefault("lang", "ko")
ss.setdefault("use_llm", True)
ss.setdefault("model", L.DEFAULT_MODEL)
ss.setdefault("round", 0)
if "_goto" in ss:
    ss.page = ss.pop("_goto")


@st.cache_data(ttl=20, show_spinner=False)
def llm_ok(model):
    return L.OllamaClient(model).available()


@st.cache_resource(show_spinner=False)
def warmup(model):
    """서버당 한 번, 백그라운드에서 모델을 GPU에 올린다 (첫 호출 17초 대기 방지, L-C07)."""
    threading.Thread(target=L.OllamaClient(model).warmup, daemon=True).start()
    return True


def client():
    if ss.use_llm and llm_ok(ss.model):
        ss.setdefault("client", L.OllamaClient(ss.model))
        if ss.client.model != ss.model:
            ss.client = L.OllamaClient(ss.model)
        return ss.client
    return None


def pl() -> Pipeline:
    if "con" not in ss:
        ss.con = sqlite3.connect(DB, check_same_thread=False)
    p = Pipeline(ss.con, client())
    return p


def now():
    return f"{sc.DEMO_DATE}T{ss.clock.strftime('%H:%M')}"


def weather():
    return {"feels_like_max": ss.get("feels", 21.0), "warning_active": ss.get("warn_active", False), "source": "manual"}


def load_scenario(name):
    s = sc.ALL[name]
    r = s["request"]
    # 같은 시나리오를 다시 돌리면 이전 실행의 잠금·증빙·서명이 남아 결과가 달라진다 → 데모 DB를 새로 만든다
    if pl().con.execute("SELECT 1 FROM permits WHERE id=?", (r["permit_id"],)).fetchone():
        reset_db()
        st.toast(f"{name} 이전 실행 기록이 있어 데모 DB를 초기화했습니다.")
    ss.text, ss.requester, ss.lang = r["request_text"], r["requester"], r.get("request_lang", "ko")
    h, m = map(int, s["now"][11:16].split(":"))
    ss.clock = time(h, m)
    ss.scenario = name
    for k in ("raw", "req", "planning", "j", "extra", "final", "docs", "photo", "notice_tr"):
        ss.pop(k, None)
    ss.round = 0
    ss._goto = PAGES[0]


def go(page):
    ss._goto = page          # 위젯 생성 후에는 page를 직접 바꿀 수 없어 다음 실행 초기에 반영


# ─────────────────────────────────────────────
# 사이드바
# ─────────────────────────────────────────────
with st.sidebar:
    st.header("🦺 AI 안전작업허가 검토관")
    st.caption("정비·화기작업 · 가상 사업장 '가온정밀'")
    st.radio("단계", PAGES, key="page", label_visibility="collapsed")
    st.divider()
    st.subheader("LLM")
    st.toggle("로컬 LLM 사용", key="use_llm")
    st.text_input("모델", key="model")
    if ss.use_llm and llm_ok(ss.model):
        warmup(ss.model)
        st.success(f"● {ss.model} 연결됨 (Ollama)")
    else:
        st.warning("○ 폴백 모드: 규칙·키워드 기반 (LIMITATIONS L-C06)")
    st.divider()
    st.subheader("시연 시나리오")
    c1, c2, c3 = st.columns(3)
    c1.button("S1", on_click=load_scenario, args=("S1",), help="5호기 점검 → 승인", width="stretch")
    c2.button("S2", on_click=load_scenario, args=("S2",), help="3호기 금형교체 → 조건부", width="stretch")
    c3.button("S3", on_click=load_scenario, args=("S3",), help="2라인 용접 → 반려", width="stretch")
    st.subheader("데모 시계·기상")
    st.caption(f"기준일 {sc.DEMO_DATE} (가상)")
    st.time_input("현재 시각", key="clock", step=300)
    st.number_input("일 최고 체감온도(℃)", key="feels", value=21.0, step=1.0,
                    help="기상청 API 미연동: 수동 입력 (L-D03)")
    st.checkbox("기상특보 발효 중", key="warn_active")
    st.divider()
    if st.button("🔄 데모 DB 초기화", width="stretch"):
        reset_db()
        st.rerun()


# ─────────────────────────────────────────────
# 공통 표시
# ─────────────────────────────────────────────
def status_banner(status):
    kind, text = STATUS_UI.get(status, ("info", status))
    getattr(st, kind)(f"**{text}**")


def results_table(j, hide_pass=True):
    rows = []
    for r in j.results:
        if hide_pass and r.outcome in ("pass", "n/a"):
            continue
        cl = r.clause if isinstance(r.clause, str) else (", ".join(r.clause) if r.clause else "")
        detail = r.reason or ", ".join(f"{k}={v}" for k, v in r.facts.items())
        rows.append({"규칙": r.rule_id, "항목": r.name, "결과": ICON[r.outcome], "등급": r.grade,
                     "근거": f"{r.source_id} {cl}".strip(), "세부": detail[:220], "출처": r.origin})
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption("표시할 항목 없음")


def trace_view(j, calls):
    with st.expander(f"🔎 실행 로그: 도구 조회 {len(j.trace)}건 · LLM 호출 {len(calls)}건", expanded=False):
        for i, t in enumerate(j.trace, 1):
            args = ", ".join(f"{k}={v}" for k, v in t["args"].items())
            st.text(f"{i:02d} 조회  {t['tool']}({args})")
        for c in calls:
            st.text(f"LLM  {c['purpose']}  " + (f"{c['seconds']}s" if c.get("seconds") is not None
                                                else f"오류 → 폴백 ({c.get('error', '')[:60]})"))


def llm_calls():
    c = client()
    return c.calls if c else []


# ─────────────────────────────────────────────
# ① 신청
# ─────────────────────────────────────────────
def page_request():
    p = pl()
    ctx = p.context()
    st.title("① 작업허가 신청")
    workers = {w["id"]: f"{w['name']} ({w['role']})" for w in ctx["workers"]}
    c1, c2 = st.columns([2, 1])
    c1.selectbox("신청자", list(workers), key="requester", format_func=workers.get)
    c2.selectbox("신청 언어", list(LANGS), key="lang", format_func=LANGS.get)
    st.text_area("신청 내용 (자유 형식)", key="text", height=90,
                 placeholder="예: 3호기 금형 교체합니다. 13시부터 15시까지.")

    if st.button("1) 신청 구조화", type="primary", disabled=not ss.text.strip()):
        with st.spinner("신청 내용을 구조화하는 중…"):
            raw, mode = p.structure(ss.text, ss.requester)
        n = p.con.execute("SELECT COUNT(*) FROM permits").fetchone()[0] + 1
        pid = sc.ALL[ss.scenario]["request"]["permit_id"] if ss.get("scenario") else f"PT-{sc.DEMO_DATE.replace('-', '')}-{n:03d}"
        ss.raw, ss.struct_mode = raw, mode
        ss.req = p.to_request(raw, ss.requester, ss.text, ss.lang, sc.DEMO_DATE, pid)
        for k in ("planning", "j", "extra", "final", "docs"):
            ss.pop(k, None)

    if "req" not in ss:
        return
    req = ss.req
    st.divider()
    st.subheader("구조화 결과")
    mode_name = {"llm": f"LLM ({ss.model})", "fallback": "폴백(키워드)",
                 "fallback:error": "LLM 오류 → 폴백(키워드)"}.get(ss.struct_mode, ss.struct_mode)
    st.caption(f"방식: {mode_name} · 허가번호 {req['permit_id']}")
    if req.get("_notes"):
        st.warning("LLM 출력 정정 (등록 데이터와 대조): " + " / ".join(req["_notes"]))
    if ss.raw.get("summary_ko"):
        summary = ss.raw["summary_ko"]
        for w in ctx["workers"]:                    # LLM 요약의 작업자 ID(W001)를 이름으로 보여 준다
            summary = re.sub(rf"\b{w['id']}\b", w["name"], summary)
        st.info(summary)
    show = {k: v for k, v in req.items() if not k.startswith("_") and k not in ("request_text",)}
    with st.expander("구조화 필드 (엔진 입력)", expanded=False):
        st.json(show)
    if req.get("_out_of_scope"):
        st.error("허가 검토 대상이 아닙니다: " + req["_out_of_scope"])
        return
    if req.get("start") and req["start"] < now():
        st.warning(f"작업 시작 시각({req['start'][11:]})이 현재 시각({now()[11:]})보다 이릅니다. "
                   "허가 없이 이미 작업을 시작했거나 시각을 잘못 적은 것일 수 있습니다. 신청 내용을 확인하세요.")

    qs = p.pending_questions(req)
    if qs:
        st.subheader(f"2) 되묻기: 확인이 필요한 항목 {len(qs)}개")
        st.caption("신청문에 없는 정보는 추측하지 않고 신청자에게 묻습니다 (DP-03).")
        with st.form("questions"):
            answers = {}
            for q in qs:
                label = q["question"] if q["question"] == q["question_ko"] else f"{q['question']}  \n({q['question_ko']})"
                answers[q["field"]] = question_widget(q, label, ctx)
            if st.form_submit_button("답변 반영", type="primary"):
                for f, v in answers.items():
                    p.answer(req, f, v)
                st.rerun()
        return

    st.success("필수 정보가 모두 확인되었습니다.")
    st.subheader("3) 허가 전 점검 14항목 (KOSHA C-C-49-2026 6.1)")
    if st.button("14항목 계획 수립", type="primary" if "planning" not in ss else "secondary"):
        with st.spinner("점검 계획을 세우는 중…"):
            ss.planning, ss.plan_mode = p.plan(req, now())
    if "planning" not in ss:
        return
    mode_txt = {"hybrid": "데이터 확정 + LLM이 미확정 항목 보완", "data": "전 항목 데이터로 확정",
                "fallback": "데이터 확정 (LLM 미연결: 미확정 항목은 모름)",
                "fallback:error": "데이터 확정 (LLM 오류: 미확정 항목은 모름)"}.get(ss.plan_mode, ss.plan_mode)
    st.caption(f"방식: {mode_txt} · [데이터] 항목은 잠겨 있음(LLM·사람 모두 수정 불가) · [LLM] 항목은 확인 후 수정 가능")
    opts = ["해당", "비해당", "모름"]
    n_unknown = sum(1 for v in ss.planning.values() if v["answer"] == "모름")
    if n_unknown:
        st.warning(f"'모름' {n_unknown}개: 현장 확인 후 해당/비해당으로 확정하세요. 남아 있으면 서명할 수 없습니다 (L-S10).")
    with st.form("plan_form"):
        chosen = {}
        for k, v in sorted(ss.planning.items(), key=lambda x: int(x[0])):
            c1, c2, c3 = st.columns([3, 1.2, 4])
            mark = "⚠ " if v["answer"] == "모름" else ""
            c1.markdown(f"{mark}**{k}.** {L.PLAN_ITEMS[int(k)]}")
            chosen[k] = c2.selectbox(f"판단 {k}", opts, index=opts.index(v["answer"]), key=f"plan_{k}_{ss.round}",
                                     label_visibility="collapsed", disabled=v["reason"].startswith("[데이터]"))
            c3.caption(v["reason"])
        submitted = st.form_submit_button("4) 검토 실행", type="primary")
    if submitted:
        for k, a in chosen.items():
            if a != ss.planning[k]["answer"]:
                ss.planning[k] = {"answer": a, "reason": f"[사람 확정] {ss.requester}"}
        if run_review(p):
            go(PAGES[1])
            st.rerun()


def question_widget(q, label, ctx):
    t, f = q["type"], q["field"]
    if t == "bool":
        v = st.radio(label, ["예", "아니오"], horizontal=True, index=None, key=f"q_{f}")
        return None if v is None else v == "예"
    if t == "int06":
        return st.number_input(label, 0, 6, 0, key=f"q_{f}")
    if t == "minutes":
        return st.number_input(label, 0, 240, 30, step=10, key=f"q_{f}")
    if t == "time":
        return st.time_input(label, time(13, 0), key=f"q_{f}").strftime("%H:%M")
    if t == "choice":
        return st.selectbox(label, q["choices"], key=f"q_{f}")
    if t == "equipment":
        names = {e["id"]: f"{e['id']} {e['name']}" for e in ctx["equipment"]} | {
            P.NO_EQUIPMENT: "대상 설비 없음 (설비 정비가 아님)"}
        return st.selectbox(label, list(names), key=f"q_{f}", format_func=names.get)
    if t == "zone":
        names = {z["id"]: f"{z['id']} {z['name']}" for z in ctx["zones"]}
        return st.selectbox(label, list(names), key=f"q_{f}", format_func=names.get)
    if t == "crew":
        others = {w["id"]: f"{w['name']} ({w['role']})" for w in ctx["workers"] if w["id"] != ss.requester}
        return st.multiselect(label, list(others), default=[d for d in q.get("default") or [] if d in others],
                              format_func=others.get, key=f"q_{f}")
    if t == "worker_or_none":
        opts = ["없음"] + [w["id"] for w in ctx["workers"]]
        names = {w["id"]: w["name"] for w in ctx["workers"]} | {"없음": "없음 / Không có / None"}
        return st.selectbox(label, opts, key=f"q_{f}", format_func=names.get)
    return st.text_input(label, key=f"q_{f}")


def run_review(p):
    for key in ("final", "docs", "signed_by", "signed_at", "notice_tr"):
        ss.pop(key, None)
    try:
        with st.spinner("상태 조회 → 규칙 판정 → 모순 탐지 중…"):
            ss.j, ss.extra = p.review(ss.req, ss.planning, now(), weather=weather())
    except ValueError as e:          # 엔진 입구 검증 (등록되지 않은 ID·시각 오류 등)
        st.error(str(e))
        return False
    ss.round += 1
    ss.pop("notice_tr", None)
    return True


# ─────────────────────────────────────────────
# ② 검토·조치
# ─────────────────────────────────────────────
def page_review():
    st.title("② 검토·조치")
    if "j" not in ss:
        st.info("① 신청에서 검토를 실행하세요.")
        return
    p, j, req = pl(), ss.j, ss.req
    st.caption(f"허가번호 {j.permit_id} · 검토 {ss.round}회차 · {j.now[11:]}")
    status_banner(j.status)
    trace_view(j, llm_calls())

    hide = st.toggle("통과·비적용 항목 숨기기", value=True)
    results_table(j, hide)
    cnt = {}
    for r in j.results:
        cnt[r.outcome] = cnt.get(r.outcome, 0) + 1
    st.caption(" · ".join(f"{ICON[k]} {v}" for k, v in cnt.items()))

    if ss.extra["findings"]:
        st.subheader("신청 기재와 현장 데이터 불일치")
        for f in ss.extra["findings"]:
            st.warning(f"**{f['claim']}** ↔ {f['evidence']}  \n(탐지: {'규칙' if f.get('by') == 'rule' else 'LLM'} · "
                       f"LLM 조정은 최대 사람 판단까지, DP-01)")
    if ss.extra["alternatives"]:
        st.subheader("대안")
        for a in ss.extra["alternatives"]:
            st.markdown(md(f"- **{a['rule']}**: {a['text']}"))
    if j.missing_fields:
        st.warning("추가 확인 필요 필드: " + ", ".join(FIELD_LABEL_KO.get(f, f) for f in j.missing_fields))

    conds = [r for r in j.results if r.outcome == "conditional"]
    if conds:
        st.subheader("조치·증빙 제출 → 재검증")
        for r in conds:
            with st.container(border=True):
                st.markdown(md(f"**{r.rule_id} {r.name}** · 요구: {', '.join(r.conditions)}"))
                evidence_ui(p, r, req)
    if j.status in ("conditional", "human_review", "approved", "rejected"):
        if st.button("🔁 재검증 (처음부터 다시 조회)", type="primary" if conds else "secondary"):
            if run_review(p):
                st.rerun()
    if any(r.rule_id == "PLAN" and r.outcome == "human_review" for r in j.results):
        st.info("허가 전 점검 14항목 중 '모름' 항목을 ① 신청 화면의 표에서 확정한 뒤 '검토 실행'을 다시 누르세요.")
        st.button("① 14항목 확정하러 가기", on_click=go, args=(PAGES[0],), key="to_plan")
    elif j.status in ("approved", "human_review"):
        st.button("③ 서명 단계로", on_click=go, args=(PAGES[2],))

    st.subheader("작업자 안내")
    docs = p.documents(j, req)
    lang = req.get("request_lang", "ko")
    st.text(docs["notice_ko"])
    if lang != "ko":
        if st.button(f"번역: {LANGS[lang]}"):
            with st.spinner("번역 중…"):
                ss.notice_tr = p.translate(docs["notice_ko"], lang)
        if ss.get("notice_tr"):
            text, mode = ss.notice_tr
            if text:      # 한 줄 조건식은 Streamlit 매직이 반환값(DeltaGenerator 도움말)까지 그린다
                st.text(text)
            else:
                st.caption("LLM 미연결: 번역 불가 (L-C04)")


def evidence_ui(p, r, req):
    spec = p.EVIDENCE.get(r.rule_id)
    key = f"ev_{r.rule_id}_{ss.round}"
    if not spec:
        st.caption("이 규칙의 증빙 처리기가 없습니다 (L-S05).")
        return
    kind = spec["kind"]
    if kind == "photo_lock":
        pts = r.facts.get("lockable_not_locked", [])
        point = st.selectbox("차단점", pts, key=key + "pt")
        src = st.radio("사진", ["합성 예시: 자물쇠+꼬리표", "합성 예시: 꼬리표만", "파일 업로드"], horizontal=True, key=key + "src")
        up = st.file_uploader("증빙 사진", type=["png", "jpg", "jpeg"], key=key + "up") if src == "파일 업로드" else None
        if st.button("사진 제출·판독", key=key + "go"):
            if src == "파일 업로드" and up is None:
                st.error("사진을 올리세요.")
                return
            if up:
                data, name = up.getvalue(), up.name
            else:
                name = "lock_and_tag.png" if "자물쇠" in src else "tag_only.png"
                data = (SAMPLES / name).read_bytes()
            EVID_DIR.mkdir(parents=True, exist_ok=True)
            import uuid
            path = EVID_DIR / f"{uuid.uuid4().hex}{Path(name).suffix.lower()}"
            path.write_bytes(data)
            with st.spinner("VLM 판독 중…"):
                ss.photo = {**p.submit_photo_lock(req, point, data, str(path.relative_to(ROOT)), now()),
                            "point": point, "path": str(path)}
        ph = ss.get("photo")
        if ph and ph["point"] == point:
            st.image(ph["path"], width=240)
            (st.success if ph["registry_updated"] else st.warning)(
                f"판독: {ph['summary']}  \n→ " + ("LOTO 대장 갱신됨. 재검증하세요." if ph["registry_updated"]
                                                  else "자동 반영 안 됨: 허가권자 확인 필요 (VLM은 보조, DP-02)"))
            if not ph["registry_updated"]:
                c1, c2 = st.columns(2)
                if c1.button("허가권자: 사진상 잠금 확인", key=key + "ok"):
                    p.confirm_photo(ph["evidence_id"], point, True, now(), "W002")
                    ph["registry_updated"] = True
                    st.rerun()
                if c2.button("허가권자: 잠금 아님", key=key + "no"):
                    p.confirm_photo(ph["evidence_id"], point, False, now(), "W002")
                    st.rerun()
    elif kind == "gas":
        c1, c2 = st.columns(2)
        lel = c1.number_input("LEL %", 0.0, 100.0, 4.0, key=key + "lel")
        o2 = c2.number_input("O₂ %", 0.0, 30.0, 20.9, key=key + "o2")
        if st.button("재측정값 제출", key=key + "go"):
            p.submit_gas(req, lel, o2, now(), "W002")
            st.success("가스 측정값 기록됨. 재검증하세요.")
    elif kind == "loto_check":
        m = st.text_input("확인 방법", "기동 스위치 시동 시도 → 무반응", key=key + "m")
        released = st.radio("잔류에너지 제거 결과", ["미확인", "완료", "실패"], key=key + "released")
        verified = st.radio("차단 검증 결과", ["미확인", "성공", "실패"], key=key + "verified")
        if st.button("차단 확인 기록", key=key + "go"):
            if "미확인" in (released, verified):
                st.error("두 확인 결과를 각각 선택하세요.")
                return
            p.submit_loto_check(req, m, now(), req["requester"],
                                 stored_energy_released=released == "완료", isolation_verified=verified == "성공")
            st.success("기록됨. 재검증하세요.")
    elif kind == "procedure":
        t = st.text_input("절차서 제목", f"{req.get('equipment_id')} LOTO 절차서", key=key + "t")
        if st.button("절차서 등록", key=key + "go"):
            p.submit_procedure(req, t, now())
            st.success("등록됨. 재검증하세요.")
    elif kind == "field":
        if st.button(f"✔ {spec['label']}", key=key + "go"):
            p.submit_field(req, r.rule_id, now())
            st.success("반영됨. 재검증하세요.")
    elif kind == "times":
        c1, c2 = st.columns(2)
        s = c1.time_input("시작", time.fromisoformat(req["start"][11:16]), key=key + "s")
        e = c2.time_input("종료", time.fromisoformat(req["end"][11:16]), key=key + "e")
        if st.button("시간 수정", key=key + "go"):
            p.answer(req, "start_hhmm", s.strftime("%H:%M"))
            p.answer(req, "end_hhmm", e.strftime("%H:%M"))
            st.success("수정됨. 재검증하세요.")
    elif kind == "zone":
        z = r.facts.get("equipment_zone")
        if st.button(f"작업 장소를 {z}로 정정", key=key + "go"):
            req["zone_id"] = z
            st.success("정정됨. 재검증하세요.")
    elif kind == "permit":
        signers = [dict(w) for w in p.con.execute("SELECT id, name FROM workers WHERE can_sign_permit=1")]
        who = st.selectbox("보충허가 서명자", [w["id"] for w in signers],
                            format_func=lambda x: next(w["name"] for w in signers if w["id"] == x), key=key + "signer")
        checked = st.checkbox("대상 설비의 정전·차단 안전조치를 현장에서 확인했습니다", key=key + "checked")
        if st.button("사람 확인·서명 후 보충허가 발급", disabled=not checked, key=key + "go"):
            try:
                p.submit_supplementary_permit(req, now(), who, verified=checked)
                st.success("사람 서명으로 발급됨. 재검증하세요.")
            except (ValueError, PermissionError) as e:
                st.error(str(e))


# ─────────────────────────────────────────────
# ③ 서명·발행
# ─────────────────────────────────────────────
def page_sign():
    st.title("③ 허가권자 서명·발행")
    if "j" not in ss:
        st.info("검토 결과가 없습니다.")
        return
    p, j, req = pl(), ss.j, ss.req
    status_banner(ss.get("final") or j.status)
    ctx = p.context()
    names = {w["id"]: f"{w['name']} ({w['role']}){' · 서명권자' if w['can_sign_permit'] else ''}" for w in ctx["workers"]}
    signer = st.selectbox("서명자", list(names), index=1, format_func=names.get, disabled="final" in ss)

    if "final" not in ss:
        if any(r.rule_id == "PLAN" and r.outcome == "human_review" for r in j.results):
            st.warning("허가 전 점검 14항목에 '모름'이 남아 있습니다. ① 신청에서 표를 확정하고 재검증하세요 (L-S10).")
            st.button("① 14항목 확정하러 가기", on_click=go, args=(PAGES[0],))
            return
        pending = [r for r in j.results if r.outcome == "human_review"]
        res = {}
        if pending:
            st.subheader(f"사람 판단 항목 {len(pending)}개")
            for r in pending:
                with st.container(border=True):
                    st.markdown(md(f"**{r.rule_id} {r.name}** 〔{r.grade}〕  \n{r.reason or r.facts}"))
                    d = st.radio("결정", ["수용(허가)", "불수용(반려)"], index=None, horizontal=True, key=f"hr_{r.rule_id}")
                    note = st.text_input("결정 사유 (필수)", key=f"hn_{r.rule_id}", placeholder="예: 정비 범위에 진동 원인 점검 포함 확인")
                    if d:
                        res[r.rule_id] = {"decision": "accept" if d.startswith("수용") else "reject", "note": note}
        if j.status == "conditional":
            st.warning("조건부 항목이 남아 있어 서명할 수 없습니다. ②에서 조치 후 재검증하세요.")
        if j.status == "rejected":
            st.info("반려 판정입니다. 서명하면 반려가 확정되고 허가서는 발행되지 않습니다. 대안을 반영해 새로 신청하세요.")
        label = "✍ 반려 확정 서명" if j.status == "rejected" else "✍ 서명 (허가서 발행)"
        if st.button(label, type="primary", disabled=j.status == "conditional"):
            try:
                ss.signed_by, ss.signed_at = signer, now()
                ss.final = p.judge.sign(j, signer, ss.signed_at, res)
                ss.docs = p.documents(j, req, ss.final, res)
                st.rerun()
            except (PermissionError, ValueError) as e:
                st.error(str(e))
        return

    stored = dict(p.con.execute("SELECT * FROM permits WHERE id=?", (j.permit_id,)).fetchone())
    docs = p.documents(j, req, stored["status"])
    status_banner(stored["status"])
    st.subheader("허가서" if stored["status"] == "issued" else "검토·서명 기록")
    signer_name = next(w["name"] for w in ctx["workers"] if w["id"] == stored["signed_by"])
    signed = docs["permit_md"].replace("- 허가권자: ________ (서명 전까지 효력 없음)",
                                       f"- 허가권자: {signer_name} (서명 {stored['signed_at'].replace('T', ' ')})")
    st.markdown(md(signed))
    st.download_button("허가서 내려받기 (.md)", signed, file_name=f"{j.permit_id}.md")
    if stored["status"] in ("suspended", "closed"):
        st.warning("허가 상태: " + stored["status"] + " — 기존 허가서의 작업 효력은 없습니다.")
    if stored["status"] == "issued":
        st.subheader("서면 게시문")
        st.code(docs["posting"], language=None)
    st.subheader("작업자 안내")
    st.text(docs["notice_ko"])
    lang = req.get("request_lang", "ko")
    if lang != "ko" and st.button(f"번역: {LANGS[lang]}", key="tr_final"):
        with st.spinner("번역 중…"):
            text, _ = p.translate(docs["notice_ko"], lang)
        if text:      # 한 줄 조건식은 Streamlit 매직이 반환값(DeltaGenerator 도움말)까지 그린다
            st.text(text)
        else:
            st.caption("LLM 미연결: 번역 불가 (L-C04)")
    if stored["status"] == "issued":
        st.button("④ 작업 감시로", on_click=go, args=(PAGES[3],))


# ─────────────────────────────────────────────
# ④ 작업 감시
# ─────────────────────────────────────────────
def page_monitor():
    st.title("④ 작업 감시")
    p = pl()
    rows = [dict(r) for r in p.con.execute(
        "SELECT id, type, task, zone_id, equipment_id, start_ts, end_ts, status FROM permits "
        "WHERE status IN ('issued','suspended','closed') AND request_json IS NOT NULL ORDER BY start_ts")]
    if not rows:
        st.info("발행된 허가가 없습니다.")
        return
    pid = st.selectbox("허가", [r["id"] for r in rows],
                       format_func=lambda x: next(f"{r['id']} · {r['task']} · {r['status']}" for r in rows if r["id"] == x))
    r = next(x for x in rows if x["id"] == pid)
    st.caption(f"{r['type']} · {r['start_ts'][11:]}~{r['end_ts'][11:]} · 현재 {now()[11:]}")
    st.markdown("**이벤트 주입 (시연용)**")
    c1, c2, c3 = st.columns(3)
    if r["equipment_id"] and c1.button("설비 가동 감지 (전류 18A)"):
        p.con.execute("INSERT INTO sensor_readings (equipment_id, ts, current_a, vib_rms) VALUES (?,?,18,2.0)",
                      (r["equipment_id"], now()))
        p.con.commit()
    if r["equipment_id"] and c2.button("잠금 해제 감지"):
        p.con.execute("UPDATE isolation_points SET locked=0, updated_at=? WHERE equipment_id=? AND lockable=1",
                      (now(), r["equipment_id"]))
        p.con.commit()
    if c3.button("가스 LEL 12% 측정"):
        p.con.execute("INSERT INTO gas_readings (zone_id, ts, lel_percent, o2) VALUES (?,?,12,20.9)", (r["zone_id"], now()))
        p.con.commit()
    if st.button("감시 실행", type="primary"):
        alerts, trace = p.monitor(pid, now())
        # 중지되면 상태 표시를 갱신하려고 다시 그리므로, 결과를 저장해 두고 아래에서 보여 준다
        ss.monitor_result = {"pid": pid, "at": now(), "alerts": alerts, "tools": [t["tool"] for t in trace]}
        if any(a["level"] == "stop" for a in alerts) and r["status"] == "issued":
            st.rerun()
    res = ss.get("monitor_result")
    if res and res["pid"] == pid:
        if r["status"] == "suspended":
            st.error(f"**허가 중지됨** ({res['at'][11:]} 감시) — 새 허가번호로 재신청·재검토 필요")
        if not res["alerts"]:
            st.success("이상 없음")
        for a in res["alerts"]:
            {"stop": st.error, "warn": st.warning, "info": st.info}[a["level"]](f"{a['text']}  〔{a['source']}〕")
        st.caption("조회: " + ", ".join(res["tools"]))


# ─────────────────────────────────────────────
# ⑤ 기록 / ⑥ 규칙·한계
# ─────────────────────────────────────────────
def page_records():
    st.title("⑤ 기록 (감사 로그·기억)")
    con = pl().con
    permits = pd.read_sql("SELECT id, type, task, requester_id, start_ts, end_ts, status, signed_by FROM permits", con)
    st.dataframe(permits, hide_index=True, width="stretch")
    pid = st.selectbox("감사 로그 허가번호", permits["id"].tolist()[::-1])
    st.dataframe(pd.read_sql("SELECT ts, step, tool, rule_id, source_id, result, detail FROM audit_log "
                             "WHERE permit_id=? ORDER BY id", con, params=(pid,)), hide_index=True, width="stretch")
    c1, c2 = st.columns(2)
    c1.markdown("**설비 이력 (기억)**")
    c1.dataframe(pd.read_sql("SELECT * FROM equipment_events ORDER BY ts DESC", con), hide_index=True)
    c2.markdown("**증빙**")
    c2.dataframe(pd.read_sql("SELECT permit_id, rule_id, image_path, vlm_result, human_confirmed, submitted_at "
                             "FROM evidence ORDER BY id DESC", con), hide_index=True)


def page_rules():
    st.title("⑥ 규칙·근거·한계")
    rb = RuleBook()
    rows = []
    for r in rb.rules:
        s = rb.sources[r["source"]]
        cl = r.get("clause")
        rows.append({"규칙": r["id"], "항목": r["name"], "불통과 시": ICON[r["on_fail"]], "등급": s["grade"],
                     "근거": f"{s['title']} {cl if isinstance(cl, str) else (', '.join(cl) if cl else '')}"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption("등급별 판정 상한: " + ", ".join(f"{g}→{a}" for g, a in rb.meta["grade_policy"].items()))
    with st.expander("근거 원문 인용"):
        for sid, s in rb.sources.items():
            st.markdown(md(f"**{sid}** {s['title']} ({s['grade']}, {s.get('version', '')})  \n"
                           f"{s.get('quote') or json.dumps(s.get('clauses', {}), ensure_ascii=False)}"))
    st.divider()
    st.markdown(md(LIMITS.read_text(encoding="utf-8")) if LIMITS.exists() else "LIMITATIONS.md 없음")


{PAGES[0]: page_request, PAGES[1]: page_review, PAGES[2]: page_sign, PAGES[3]: page_monitor,
 PAGES[4]: page_records, PAGES[5]: page_rules}[ss.page]()
