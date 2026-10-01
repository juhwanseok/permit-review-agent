"""규칙별 판정 함수. permit_rules.yaml의 expr(의사 코드)을 구현한 것이다.

각 함수는 Check(applies, passed, facts)를 돌려준다.
- applies/passed가 None이면 '데이터 없음'이다. judge가 DP-03에 따라 사람 판단으로 보낸다.
- 판정 강도(on_fail)와 출처는 YAML에서 읽는다. 여기서는 사실 확인만 한다.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

GAS_MAX_AGE = timedelta(hours=4)   # P-35 5.2(1)·기준규칙 제296조1호: 작업 전 측정, 4시간마다 재측정

CHECKS = {}

DIE_TASKS = {"금형부착", "금형해체", "금형조정", "금형교체"}
DIE_INSTALL_TASKS = {"금형부착", "금형교체"}
SPARK_BARRIERS = {"인증방화포", "비산방지덮개"}


@dataclass
class Check:
    applies: object            # True / False / None(모름)
    passed: object = None      # True / False / None(모름)
    facts: dict = field(default_factory=dict)


def check(rule_id):
    def deco(fn):
        CHECKS[rule_id] = fn
        return fn
    return deco


class Ctx:
    def __init__(self, tools, request, planning):
        self.t = tools
        self.req = request
        self.plan = planning
        self.missing = []          # 신청에서 빠진 필드 → 되묻기 목록

    def need(self, key):
        v = self.req.get(key)
        if v is None and key not in self.missing:
            self.missing.append(key)
        return v

    @property
    def workers(self):
        return self.req.get("workers") or []

    def eq(self):
        eid = self.req.get("equipment_id")
        return self.t.equipment(eid) if eid else None

    def zone(self):
        return self.t.zone(self.req["zone_id"])

    def plan_item(self, n):
        return self.plan.get(str(n), {}).get("answer")


def _all_have(ctx, code):
    tr = ctx.t.trainings(ctx.workers)
    lacking = [w for w, codes in tr.items() if code not in codes]
    return not lacking and bool(ctx.workers), {"lacking": lacking, "code": code}


def _hours(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 3600


# ── 공통 ──
@check("C-01")
def c01(ctx):
    e = ctx.eq()
    if e is None:
        return Check(False)
    return Check(True, e["zone_id"] == ctx.req["zone_id"],
                 {"equipment_zone": e["zone_id"], "request_zone": ctx.req["zone_id"]})


@check("C-02")
def c02(ctx):
    s = ctx.t.site()
    try:
        start, end = datetime.fromisoformat(ctx.req["start"]), datetime.fromisoformat(ctx.req["end"])
    except (KeyError, TypeError, ValueError):
        return Check(True, None, {"reason": "작업 시각 형식 오류"})
    problems = []
    if start.date() != end.date():
        problems.append("당일을 넘김")
    if end <= start:
        problems.append("종료가 시작보다 빠르거나 같음")
    if start.strftime("%H:%M") < s["shift_start"]:
        problems.append(f"근무 시작({s['shift_start']}) 전 시작")
    if end.strftime("%H:%M") > s["shift_end"]:
        problems.append(f"근무 종료({s['shift_end']}) 후 종료")
    return Check(True, not problems, {"start": ctx.req["start"], "end": ctx.req["end"],
                                      "shift": f"{s['shift_start']}~{s['shift_end']}", "problems": problems})


@check("C-03")
def c03(ctx):
    p = ctx.t.procedure("작업", ctx.req["task"], ctx.req.get("equipment_id"))
    ok = p is not None or bool(ctx.req.get("risk_assessment_done"))
    return Check(True, ok, {"procedure": p["title"] if p else None})


@check("C-04")
def c04(ctx):
    w = ctx.t.weather()
    if w.get("feels_like_max") is None:
        return Check(None)
    if w["feels_like_max"] < 33:
        return Check(False, facts={"feels_like_max": w["feels_like_max"]})
    ok = bool(ctx.req.get("rest_plan_2h_20m") or ctx.req.get("personal_cooling_gear"))
    return Check(True, ok, {"feels_like_max": w["feels_like_max"]})


@check("C-05")
def c05(ctx):
    w = ctx.t.weather()
    if w.get("feels_like_max") is None:
        return Check(None)
    # 시스템이 감사 로그에 일자별 체감온도·조치를 기록하므로 적용 시 항상 충족
    return Check(w["feels_like_max"] >= 31, True, {"feels_like_max": w["feels_like_max"]})


@check("C-06")
def c06(ctx):
    if not ctx.zone()["outdoor"]:
        return Check(False)
    w = ctx.t.weather()
    if w.get("feels_like_max") is None:
        return Check(None)
    f = w["feels_like_max"]
    if f < 35:
        return Check(False, facts={"feels_like_max": f})
    overlaps_afternoon = ctx.req["start"][11:16] < "17:00" and ctx.req["end"][11:16] > "14:00"
    ok = not (f >= 38 and not ctx.req.get("emergency")) and not overlaps_afternoon
    return Check(True, ok, {"feels_like_max": f, "overlaps_14_17": overlaps_afternoon})


@check("C-07")
def c07(ctx):
    if not ctx.zone()["outdoor"]:
        return Check(False)
    w = ctx.t.weather()
    if w.get("warning_active") is None:
        return Check(None)
    return Check(bool(w["warning_active"]), False, {"warning_active": w["warning_active"]})


# ── 정비작업 ──
@check("M-01")
def m01(ctx):
    r = ctx.t.latest_sensor(ctx.req["equipment_id"])
    if r is None or r.get("stale"):
        return Check(True, None, {"sensor": r})
    return Check(True, r["current_a"] == 0, {"current_a": r["current_a"], "ts": r["ts"]})


@check("M-02a")
def m02a(ctx):
    pts = ctx.t.isolation_points(ctx.req["equipment_id"])
    bad = [p["id"] for p in pts if not (p["locked"] or p["tagged"])]
    return Check(True, bool(pts) and not bad, {"unsecured": bad})


@check("M-02b")
def m02b(ctx):
    pts = ctx.t.isolation_points(ctx.req["equipment_id"])
    bad = [p["id"] for p in pts if p["lockable"] and not p["locked"]]
    return Check(True, not bad, {"lockable_not_locked": bad})


@check("M-03")
def m03(ctx):
    e = ctx.eq()
    if not e["has_stored_energy"]:
        return Check(False)
    c = ctx.t.latest_loto_check(e["id"])
    if c is None:
        return Check(True, None, {"stored_energy_types": e["stored_energy_types"]})
    return Check(True, bool(c["stored_energy_released"]), {"stored_energy_types": e["stored_energy_types"]})


@check("M-04")
def m04(ctx):
    """차단 확인은 마지막 잠금·표지 변경 **이후**에 해야 유효하다 (B-M-25 6.4 → 6.5 → 6.6 순서)."""
    eid = ctx.req["equipment_id"]
    c = ctx.t.latest_loto_check(eid)
    if c is None:
        return Check(True, None)
    changed = [p["updated_at"] for p in ctx.t.isolation_points(eid) if p["locked"] or p["tagged"]]
    last_change = max(changed) if changed else None
    in_order = last_change is None or c["checked_at"] >= last_change
    return Check(True, bool(c["isolation_verified"]) and in_order,
                 {"method": c["verify_method"], "checked_at": c["checked_at"], "last_isolation_change": last_change,
                  "order_ok": in_order})


@check("M-05")
def m05(ctx):
    p = ctx.t.procedure("LOTO", eq_id=ctx.req["equipment_id"])
    return Check(True, p is not None, {"procedure": p["title"] if p else None})


@check("M-06")
def m06(ctx):
    if ctx.req["task"] not in DIE_TASKS:
        return Check(False)
    body = ctx.need("body_in_danger_zone")
    if body is None:
        return Check(None)
    if not body:
        return Check(False)
    used = ctx.need("safety_block_used")
    return Check(True, None if used is None else bool(used))


@check("M-07")
def m07(ctx):
    if ctx.req["task"] not in DIE_INSTALL_TASKS:
        return Check(False)
    n = ctx.need("die_precheck_completed")
    return Check(True, None if n is None else n == 6, {"completed": n})


@check("M-08")
def m08(ctx):
    e, s = ctx.eq(), ctx.t.site()
    if not (s["power_press_count"] >= 5 and e["type"] == "press"):
        return Check(False)
    ok, facts = _all_have(ctx, "B5-11")
    return Check(True, ok, {**facts, "power_press_count": s["power_press_count"]})


@check("M-09")
def m09(ctx):
    e = ctx.eq()
    if e["zone_bc_limit"] is None:
        return Check(False)
    r = ctx.t.last_running_vib(e["id"])
    if r is None:
        return Check(True, None, {"reason": "오늘 운전 기록 없음"})
    v = r["vib_rms"]
    zone = "A/B" if v < e["zone_bc_limit"] else ("C" if v < e["zone_cd_limit"] else "D")
    return Check(True, v < e["zone_bc_limit"],
                 {"vib_rms": v, "zone": zone, "bc": e["zone_bc_limit"], "cd": e["zone_cd_limit"]})


# ── 화기작업 ──
def _combustibles(ctx):
    return ctx.t.combustibles_within(ctx.req["zone_id"], 11)


@check("H-01")
def h01(ctx):
    near, z = _combustibles(ctx), ctx.zone()
    exempt = bool(z["routine_hot_work"] and z["alarm_and_extinguisher"])
    if not near or exempt:
        return Check(False, facts={"combustibles": near, "exempt": exempt})
    if "fire_watch" not in ctx.req:          # 키가 없으면 모름, null이면 미지정
        ctx.missing.append("fire_watch")
        return Check(True, None, {"combustibles": near})
    return Check(True, bool(ctx.req["fire_watch"]), {"combustibles": near, "fire_watch": ctx.req["fire_watch"]})


@check("H-02")
def h02(ctx):
    v = ctx.need("extinguisher_placed")
    return Check(True, None if v is None else bool(v))


@check("H-03")
def h03(ctx):
    v = ctx.need("spark_barrier")
    return Check(True, None if v is None else v in SPARK_BARRIERS, {"spark_barrier": v})


@check("H-04")
def h04(ctx):
    items = (ctx.plan_item(6), ctx.plan_item(8))
    if "해당" not in items:
        return Check(False)
    v = ctx.need("ventilation_done")
    return Check(True, None if v is None else bool(v))


@check("H-05")
def h05(ctx):
    # 게시문은 judge가 허가서와 함께 자동 생성한다
    return Check(True, True, {"note": "서면 게시문 자동 생성"})


@check("H-06")
def h06(ctx):
    v = ctx.need("excess_combustibles_removed")
    return Check(True, None if v is None else bool(v))


@check("H-07")
def h07(ctx):
    if ctx.zone()["location_type"] not in ("지하작업장", "가스도관굴착"):
        return Check(False)
    g = ctx.t.latest_gas(ctx.req["zone_id"])
    if g is None or g["lel_percent"] is None:
        return Check(True, None)
    fresh, age = _gas_fresh(ctx, g)
    return Check(True, g["lel_percent"] < 25 and fresh, {"lel_percent": g["lel_percent"], "measured_hours_before": age})


def _gas_fresh(ctx, g):
    """측정 시각이 작업 시작(이미 시작했으면 현재) 기준 4시간 이내인가."""
    ref = max(datetime.fromisoformat(ctx.req["start"]), ctx.t.now)
    age = ref - datetime.fromisoformat(g["ts"])
    return age <= GAS_MAX_AGE, round(age.total_seconds() / 3600, 1)


@check("H-08")
def h08(ctx):
    g = ctx.t.latest_gas(ctx.req["zone_id"])
    if g is None or g["lel_percent"] is None:
        return Check(True, None, {"reason": "오늘 측정값 없음"})
    fresh, age = _gas_fresh(ctx, g)
    return Check(True, g["lel_percent"] < 10 and fresh,
                 {"lel_percent": g["lel_percent"], "ts": g["ts"], "measured_hours_before": age,
                  "stale": not fresh})


@check("H-09")
def h09(ctx):
    h = _hours(ctx.req["start"], ctx.req["end"])
    if h <= 4:
        return Check(False, facts={"hours": h})
    v = ctx.need("gas_remeasure_plan_4h")
    return Check(True, None if v is None else bool(v), {"hours": h})


@check("H-10")
def h10(ctx):
    v = ctx.need("post_work_fire_watch_min")
    return Check(True, None if v is None else v >= 30, {"minutes": v})


@check("H-11")
def h11(ctx):
    if not _combustibles(ctx):
        return Check(False)
    ok, facts = _all_have(ctx, "B5-38")
    return Check(True, ok, facts)


@check("H-12")
def h12(ctx):
    if ctx.plan_item(1) != "해당":
        return Check(False)
    g = ctx.t.latest_gas(ctx.req["zone_id"])
    if g is None or None in (g["o2"], g["co2"], g["co_ppm"], g["h2s_ppm"]):
        return Check(True, None)
    ok = 18 <= g["o2"] < 23.5 and g["co2"] < 1.5 and g["co_ppm"] < 30 and g["h2s_ppm"] < 10
    return Check(True, ok, {k: g[k] for k in ("o2", "co2", "co_ppm", "h2s_ppm")})


@check("H-13")
def h13(ctx):
    if ctx.plan_item(2) != "해당":
        return Check(False)
    ps = ctx.t.permits_overlapping(ctx.req["start"], ctx.req["end"], ctx.req.get("permit_id"))
    ok = any(p["type"] == "정전작업" and p["equipment_id"] == ctx.req.get("equipment_id") for p in ps)
    return Check(True, ok)


# ── 동시작업 ──
@check("X-01")
def x01(ctx):
    z = ctx.zone()
    ps = ctx.t.permits_overlapping(ctx.req["start"], ctx.req["end"], ctx.req.get("permit_id"))
    clash = [{"permit": p["id"], "task": p["task"], "zone": p["zone_id"],
              "distance_m": round(math.hypot(p["x_m"] - z["x_m"], p["y_m"] - z["y_m"]), 1)}
             for p in ps if p["handles_combustibles"]
             and math.hypot(p["x_m"] - z["x_m"], p["y_m"] - z["y_m"]) <= 11]
    return Check(True, not clash, {"clash": clash})
