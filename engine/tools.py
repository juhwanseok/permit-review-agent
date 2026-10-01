"""상태 조회 도구. 파이프라인의 '상태 조회' 단계이며, 호출마다 실행 로그(trace)에 남는다.

실제 현장에 붙일 때는 이 모듈의 함수만 MES·LOTO·교육 시스템 연동으로 교체하면 된다.
"""
import math
import sqlite3
from datetime import datetime, timedelta

STUB_WEATHER = {"feels_like_max": 21.0, "warning_active": False, "source": "stub"}


class Tools:
    def __init__(self, con: sqlite3.Connection, now: str, weather=None, sensor_max_age_min=30):
        con.row_factory = sqlite3.Row
        self.con = con
        self.now = datetime.fromisoformat(now)
        self.today = self.now.date().isoformat()
        self._weather = weather if weather is not None else STUB_WEATHER
        self.sensor_max_age = timedelta(minutes=sensor_max_age_min)
        self.trace = []

    # ── 내부 ──
    def _log(self, tool, args, result):
        # 같은 도구·인자는 실행 로그에 한 번만 남긴다 (여러 규칙이 같은 사실을 참조함)
        if not any(t["tool"] == tool and t["args"] == args for t in self.trace):
            self.trace.append({"tool": tool, "args": args, "result": result})
        return result

    def _one(self, sql, *a):
        r = self.con.execute(sql, a).fetchone()
        return dict(r) if r else None

    def _all(self, sql, *a):
        return [dict(r) for r in self.con.execute(sql, a).fetchall()]

    def _now_s(self):
        return self.now.isoformat(timespec="minutes")

    # ── 현장·설비 ──
    def site(self):
        s = self._one("SELECT s.*, st.power_press_count FROM site s JOIN site_stats st ON st.site_id = s.id")
        return self._log("site", {}, s)

    def zone(self, zone_id):
        return self._log("site_map.zone", {"zone": zone_id}, self._one("SELECT * FROM zones WHERE id=?", zone_id))

    def equipment(self, eq_id):
        return self._log("equipment_status", {"equipment": eq_id},
                         self._one("SELECT * FROM equipment WHERE id=?", eq_id))

    def combustibles_within(self, zone_id, radius_m):
        """반경 안에 상시 가연물이 있는 구역 목록 (자기 구역 포함)."""
        z = self._one("SELECT * FROM zones WHERE id=?", zone_id)
        hits = []
        for o in self._all("SELECT * FROM zones WHERE combustibles=1"):
            d = math.hypot(o["x_m"] - z["x_m"], o["y_m"] - z["y_m"])
            if d <= radius_m:
                hits.append({"zone": o["id"], "name": o["name"], "distance_m": round(d, 1),
                             "types": o["combustible_types"]})
        return self._log("site_map.combustibles_within", {"zone": zone_id, "radius_m": radius_m}, hits)

    # ── 센서 ──
    def latest_sensor(self, eq_id):
        """now 기준 최신값. SP-02 유효시간을 넘으면 None (DP-03으로 사람 판단)."""
        r = self._one("SELECT * FROM sensor_readings WHERE equipment_id=? AND ts<=? ORDER BY ts DESC, id DESC LIMIT 1",
                      eq_id, self._now_s())
        if r and self.now - datetime.fromisoformat(r["ts"]) > self.sensor_max_age:
            r = {**r, "stale": True}
        return self._log("equipment_sensor.latest", {"equipment": eq_id}, r)

    def last_running_vib(self, eq_id):
        r = self._one("SELECT ts, vib_rms FROM sensor_readings WHERE equipment_id=? AND ts<=? AND ts>=? "
                      "AND current_a>0 ORDER BY ts DESC, id DESC LIMIT 1", eq_id, self._now_s(), self.today)
        return self._log("equipment_sensor.last_running_vib", {"equipment": eq_id}, r)

    # ── LOTO ──
    def isolation_points(self, eq_id):
        return self._log("loto_registry.points", {"equipment": eq_id},
                         self._all("SELECT * FROM isolation_points WHERE equipment_id=? ORDER BY id", eq_id))

    def latest_loto_check(self, eq_id):
        r = self._one("SELECT * FROM loto_checks WHERE equipment_id=? AND checked_at<=? AND checked_at>=? "
                      "ORDER BY checked_at DESC, id DESC LIMIT 1", eq_id, self._now_s(), self.today)
        return self._log("loto_registry.check", {"equipment": eq_id}, r)

    def procedure(self, kind, task=None, eq_id=None):
        if kind == "LOTO":
            r = self._one("SELECT * FROM procedures WHERE kind='LOTO' AND equipment_id=?", eq_id)
        else:
            r = self._one("SELECT * FROM procedures WHERE kind='작업' AND task=? "
                          "AND (equipment_id IS NULL OR equipment_id=?)", task, eq_id)
        return self._log("procedures", {"kind": kind, "task": task, "equipment": eq_id}, r)

    # ── 사람 ──
    def trainings(self, worker_ids):
        out = {w: set() for w in worker_ids}
        for w in worker_ids:
            for r in self._all("SELECT code FROM trainings WHERE worker_id=?", w):
                out[w].add(r["code"])
        return self._log("training_records", {"workers": worker_ids}, {k: sorted(v) for k, v in out.items()})

    def worker(self, worker_id):
        return self._one("SELECT * FROM workers WHERE id=?", worker_id)

    # ── 가스·허가·기상 ──
    def latest_gas(self, zone_id):
        r = self._one("SELECT * FROM gas_readings WHERE zone_id=? AND ts<=? AND ts>=? ORDER BY ts DESC, id DESC LIMIT 1",
                      zone_id, self._now_s(), self.today)
        return self._log("gas_reading", {"zone": zone_id}, r)

    def permits_overlapping(self, start, end, exclude_id=None):
        rows = self._all("SELECT p.*, z.x_m, z.y_m FROM permits p JOIN zones z ON z.id = p.zone_id "
                         "WHERE p.status IN ('issued','approved') AND p.start_ts < ? AND p.end_ts > ? "
                         "AND p.id IS NOT ?", end, start, exclude_id)
        return self._log("permits_today", {"start": start, "end": end}, rows)

    def vib_events(self, eq_id, days):
        since = (self.now - timedelta(days=days)).isoformat(timespec="minutes")
        n = self._one("SELECT COUNT(*) AS n FROM equipment_events WHERE equipment_id=? "
                      "AND event_type LIKE 'vib_zone_%' AND ts>=? AND ts<=?", eq_id, since, self._now_s())["n"]
        return self._log("memory.equipment_events", {"equipment": eq_id, "days": days}, n)

    def weather(self):
        return self._log("weather", {}, dict(self._weather))
