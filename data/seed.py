"""데모 DB 생성: 가상 사업장 '가온정밀'의 하루치 상태.

Q7 시연 시나리오 S1(승인)·S2(조건부→재검증)·S3(반려)가 재현되도록 초기 상태를 만든다.
센서값은 현재 합성값(data_source='synthetic')이며, 이후 실제 기록 데이터 재생으로 교체한다.

실행: python data/seed.py [--date YYYY-MM-DD] [--db data/permit_demo.db]
"""
import argparse
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "schema.sql"
DEFAULT_DB = HERE / "permit_demo.db"
SCHEMA_VERSION = 2

SITE = ("GAON", "가온정밀", 0, "08:30", "17:30")

# id, name, x, y, location_type, outdoor, combustibles, combustible_types, routine_hot_work, alarm_and_extinguisher
ZONES = [
    ("L1", "1라인 프레스", 0, 0, "일반", 0, 0, None, 0, 0),
    ("W2", "2라인 용접 W-2", 30, 0, "일반", 0, 0, None, 0, 0),
    ("PB", "도장 부스", 37, 0, "일반", 0, 1, "도료,신너", 0, 1),         # W2에서 7m (S3)
    ("W1", "용접 전용 부스", 30, 15, "일반", 0, 0, None, 1, 1),          # H-01 단서 적용 변형용
    ("MS", "자재창고", 0, 25, "일반", 0, 1, "파렛트,포장재", 0, 0),
    ("YD", "옥외 적치장", 60, 0, "일반", 1, 0, None, 0, 0),             # C-06/C-07 변형용
]

# ISO 20816-3 Group 2(15~300kW)·유연지지 경계. B-M-37-2026 6.2(6) 범위 안.
ZONE_LIMITS = (2.3, 4.5, 7.1)

WORKERS = [
    # id, name, role, language, hire_date, can_sign_permit
    ("W001", "김정비", "정비반장", "ko", "2015-03-02", 0),
    ("W002", "박안전", "안전관리자", "ko", "2019-07-01", 1),
    ("W003", "응우옌 반 탄", "용접공", "vi", None, 0),   # 입사 3개월: 기준일로부터 계산
    ("W004", "이도장", "도장공", "ko", "2021-05-10", 0),
    ("W005", "최용접", "용접공", "ko", "2017-09-04", 0),   # S3 대안 인력
]

TRAININGS = [
    ("W001", "B5-11", "2025-04-15"),   # 프레스 5대 이상 사업장 작업
    ("W005", "B5-38", "2025-11-20"),   # 가연물 장소 화재위험작업
    ("W005", "B5-2", "2025-11-20"),
]


def ts(d: date, hhmm: str) -> str:
    return f"{d.isoformat()}T{hhmm}"


def minutes(d: date, start: str, end: str, step: int):
    t = datetime.fromisoformat(ts(d, start))
    stop = datetime.fromisoformat(ts(d, end))
    while t <= stop:
        yield t
        t += timedelta(minutes=step)


def sensor_rows(d: date, rng: random.Random):
    """설비별 하루 센서값. 운전 구간만 전류·진동이 있고, 정지 후 0."""
    rows = []

    def run(eq, start, stop, vib_from, vib_to, amp):
        pts = list(minutes(d, start, stop, 10))
        for i, t in enumerate(pts):
            frac = i / max(len(pts) - 1, 1)
            vib = vib_from + (vib_to - vib_from) * frac + rng.uniform(-0.05, 0.05)
            rows.append((eq, t.isoformat(timespec="minutes"), round(amp + rng.uniform(-1, 1), 1), round(vib, 2)))
        return pts[-1]

    def idle(eq, after: datetime, until: str):
        t = after + timedelta(minutes=10)
        for t2 in minutes(d, t.strftime("%H:%M"), until, 10):
            rows.append((eq, t2.isoformat(timespec="minutes"), 0.0, 0.0))

    for n in (1, 2, 4, 6, 7):
        run(f"P-0{n}", "08:30", "17:30", 1.9, 2.2, 19.0)
    # S1: 5호기 정상 진동으로 10:00까지 운전 후 정지. 마지막 운전값 2.1
    last = run("P-05", "08:30", "10:00", 2.0, 2.1, 19.0)
    rows[-1] = (rows[-1][0], rows[-1][1], rows[-1][2], 2.1)
    idle("P-05", last, "17:30")
    # S2: 3호기 진동이 오전에 C영역까지 상승, 12:00 정지. 마지막 운전값 6.2
    last = run("P-03", "08:30", "12:00", 3.8, 6.2, 21.0)
    rows[-1] = (rows[-1][0], rows[-1][1], rows[-1][2], 6.2)
    idle("P-03", last, "17:30")
    return rows


def build(db_path: Path, d: date, seed: int = 7) -> Path:
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA.read_text(encoding="utf-8"))
    rng = random.Random(seed)
    morning = ts(d, "08:20")

    con.execute("INSERT INTO site VALUES (?,?,?,?,?)", SITE)
    con.executemany("INSERT INTO zones VALUES (?,?,?,?,?,?,?,?,?,?,?)", [(z[0], "GAON", *z[1:]) for z in ZONES])

    hire_tan = (d - timedelta(days=90)).isoformat()
    con.executemany("INSERT INTO workers VALUES (?,?,?,?,?,?)",
                    [(w[0], w[1], w[2], w[3], w[4] or hire_tan, w[5]) for w in WORKERS])
    con.executemany("INSERT INTO trainings VALUES (?,?,?)", TRAININGS)

    ab, bc, cd = ZONE_LIMITS
    for n in range(1, 8):
        con.execute("INSERT INTO equipment VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"P-0{n}", "GAON", f"{n}호기 프레스", "press", "L1", 22.0, 1, "유압,중력(슬라이드)", ab, bc, cd))
    con.execute("INSERT INTO equipment VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("WD-01", "GAON", "2라인 CO2 용접기", "welder", "W2", 15.0, 0, None, None, None, None))

    # 차단점: 기본은 운전 중(잠금·표지 없음). 시나리오 설비만 상태를 바꾼다.
    for n in range(1, 8):
        eq = f"P-0{n}"
        con.execute("INSERT INTO isolation_points VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"{eq}-E", eq, "전기", "주전원 차단기", 1, 0, 0, None, morning))
        con.execute("INSERT INTO isolation_points VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"{eq}-H", eq, "유압", "유압 유닛 밸브", 1, 0, 0, None, morning))
    # S1: 5호기 전부 잠금 + 표지
    con.execute("UPDATE isolation_points SET locked=1, tagged=1, key_holder='W001', updated_at=? "
                "WHERE equipment_id='P-05'", (ts(d, "10:15"),))
    # S2: 3호기 주전원은 표지만(잠금 가능한데 미잠금), 유압은 잠금
    con.execute("UPDATE isolation_points SET locked=0, tagged=1, key_holder='W001', updated_at=? WHERE id='P-03-E'",
                (ts(d, "12:40"),))
    con.execute("UPDATE isolation_points SET locked=1, tagged=1, key_holder='W001', updated_at=? WHERE id='P-03-H'",
                (ts(d, "12:40"),))

    con.executemany("INSERT INTO loto_checks (equipment_id, stored_energy_released, isolation_verified, "
                    "verify_method, checked_by, checked_at) VALUES (?,?,?,?,?,?)", [
                        ("P-05", 1, 1, "기동 스위치 시동 시도 → 무반응", "W001", ts(d, "10:20")),
                        ("P-03", 1, 1, "기동 스위치 시동 시도 → 무반응, 슬라이드 하사점", "W001", ts(d, "12:45")),
                    ])

    procs = [(f"PR-LOTO-P0{n}", f"P-0{n}", "LOTO", None, f"{n}호기 프레스 LOTO 절차서", "2026-03-10")
             for n in range(1, 8)]
    procs += [("PR-TASK-DIE", None, "작업", "금형교체", "프레스 금형 교체 작업표준서", "2026-02-01"),
              ("PR-TASK-INSP", None, "작업", "점검", "프레스 정기점검 작업표준서", "2026-02-01"),
              ("PR-TASK-WELD", None, "작업", "용접", "CO2 용접 작업표준서", "2026-02-01")]
    con.executemany("INSERT INTO procedures VALUES (?,?,?,?,?,?)", procs)

    con.executemany("INSERT INTO sensor_readings (equipment_id, ts, current_a, vib_rms) VALUES (?,?,?,?)",
                    sensor_rows(d, rng))

    con.executemany("INSERT INTO gas_readings (zone_id, ts, lel_percent, o2, co2, co_ppm, h2s_ppm, measured_by) "
                    "VALUES (?,?,?,?,?,?,?,?)", [
                        ("W2", ts(d, "13:40"), 3.0, 20.9, 0.04, 2, 0, "W002"),     # S3: 통과 수준
                        ("PB", ts(d, "13:00"), 6.0, 20.9, 0.05, 1, 0, "W004"),
                    ])

    # 당일 허가: S3와 시간이 겹치는 도장 작업
    con.execute("INSERT INTO permits (id, type, requester_id, zone_id, task, start_ts, end_ts, "
                "handles_combustibles, status, request_text, signed_by, signed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"PT-{d:%Y%m%d}-001", "일반위험작업", "W004", "PB", "범퍼 브래킷 도장",
                 ts(d, "13:00"), ts(d, "17:00"), 1, "issued", "도장 부스에서 브래킷 도장 작업", "W002", ts(d, "12:50")))
    con.execute("INSERT INTO permit_workers VALUES (?,?,?)", (f"PT-{d:%Y%m%d}-001", "W004", "작업자"))

    # 기억: 3호기는 12일 전 C영역 진동 1회 (오늘 감지 시 30일 내 2회가 됨 → SP-01)
    con.executemany("INSERT INTO equipment_events (equipment_id, ts, event_type, detail) VALUES (?,?,?,?)", [
        ("P-03", (datetime.fromisoformat(ts(d, "14:20")) - timedelta(days=12)).isoformat(timespec="minutes"),
         "vib_zone_c", "RMS 5.1mm/s, 베어링 그리스 보충"),
        ("P-05", (datetime.fromisoformat(ts(d, "09:00")) - timedelta(days=60)).isoformat(timespec="minutes"),
         "repair", "벨트 교체 (30일 창 밖)"),
    ])

    con.commit()
    con.close()
    return db_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--db", default=str(DEFAULT_DB))
    a = ap.parse_args()
    p = build(Path(a.db), date.fromisoformat(a.date))
    print(f"생성: {p} (기준일 {a.date})")
