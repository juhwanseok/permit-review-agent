"""시드 DB가 Q7 시나리오 S1~S3의 전제 사실을 담고 있는지 검사한다."""
import math
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
import seed  # noqa: E402

D = date(2026, 10, 5)
_tmp = Path(tempfile.mkdtemp()) / "t.db"
seed.build(_tmp, D)
con = sqlite3.connect(_tmp)
con.row_factory = sqlite3.Row
con.execute("PRAGMA foreign_keys = ON")


def one(sql, *args):
    return con.execute(sql, args).fetchone()


def last_running_vib(eq, before):
    r = one("SELECT vib_rms FROM sensor_readings WHERE equipment_id=? AND ts<=? AND current_a>0 "
            "ORDER BY ts DESC LIMIT 1", eq, before)
    return r["vib_rms"]


def latest_current(eq, at):
    return one("SELECT current_a FROM sensor_readings WHERE equipment_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
               eq, at)["current_a"]


def has_training(worker, code):
    return one("SELECT 1 FROM trainings WHERE worker_id=? AND code=?", worker, code) is not None


def dist(a, b):
    za, zb = one("SELECT * FROM zones WHERE id=?", a), one("SELECT * FROM zones WHERE id=?", b)
    return math.hypot(za["x_m"] - zb["x_m"], za["y_m"] - zb["y_m"])


# ── 공통 ──
def test_foreign_keys_ok():
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_press_count_triggers_b5_11():
    assert one("SELECT power_press_count FROM site_stats")["power_press_count"] >= 5


def test_zone_limits_enforced_by_schema():
    try:
        con.execute("UPDATE equipment SET zone_bc_limit=20 WHERE id='P-01'")
    except sqlite3.IntegrityError:
        con.rollback()
        return
    raise AssertionError("B-M-37 범위 밖 경계값이 저장됨")


# ── S1: 5호기 점검 → 승인 ──
def test_s1_p05_stopped_and_normal_vibration():
    at = f"{D}T10:30"
    assert latest_current("P-05", at) == 0                        # M-01
    eq = one("SELECT * FROM equipment WHERE id='P-05'")
    assert last_running_vib("P-05", at) == 2.1 < eq["zone_bc_limit"]   # M-09 B영역


def test_s1_p05_loto_complete():
    pts = con.execute("SELECT * FROM isolation_points WHERE equipment_id='P-05'").fetchall()
    assert len(pts) == 2 and all(p["locked"] and p["tagged"] for p in pts)   # M-02a, M-02b
    chk = one("SELECT * FROM loto_checks WHERE equipment_id='P-05'")
    assert chk["stored_energy_released"] and chk["isolation_verified"]      # M-03, M-04
    assert one("SELECT 1 FROM procedures WHERE equipment_id='P-05' AND kind='LOTO'")   # M-05
    assert has_training("W001", "B5-11")                                    # M-08


# ── S2: 3호기 금형교체 → 조건부 → 재검증 ──
def test_s2_main_power_tagged_only():
    e = one("SELECT * FROM isolation_points WHERE id='P-03-E'")
    assert e["lockable"] and e["tagged"] and not e["locked"]    # M-02a 통과, M-02b 조건부
    h = one("SELECT * FROM isolation_points WHERE id='P-03-H'")
    assert h["locked"]


def test_s2_vibration_zone_c():
    eq = one("SELECT * FROM equipment WHERE id='P-03'")
    v = last_running_vib("P-03", f"{D}T13:00")
    assert eq["zone_bc_limit"] <= v < eq["zone_cd_limit"]      # M-09 사람 판단
    assert latest_current("P-03", f"{D}T13:00") == 0           # M-01 통과


def test_s2_memory_one_prior_event_in_30_days():
    since = (datetime(2026, 10, 5, 13, 0) - timedelta(days=30)).isoformat(timespec="minutes")
    n = one("SELECT COUNT(*) c FROM equipment_events WHERE equipment_id='P-03' AND event_type LIKE 'vib_zone_%' "
            "AND ts>=?", since)["c"]
    assert n == 1                                               # 오늘 감지분과 합쳐 2회 → SP-01


def test_s2_die_procedure_exists():
    assert one("SELECT 1 FROM procedures WHERE kind='작업' AND task='금형교체'")   # C-03


# ── S3: 2라인 용접 → 반려 ──
def test_s3_paint_booth_within_11m():
    assert dist("W2", "PB") <= 11
    assert one("SELECT combustibles FROM zones WHERE id='PB'")["combustibles"] == 1
    w2 = one("SELECT * FROM zones WHERE id='W2'")
    assert not (w2["routine_hot_work"] and w2["alarm_and_extinguisher"])   # H-01 단서 미적용


def test_s3_overlapping_paint_permit():
    r = one("SELECT * FROM permits WHERE zone_id='PB' AND handles_combustibles=1 AND status='issued' "
            "AND start_ts < ? AND end_ts > ?", f"{D}T16:00", f"{D}T14:00")
    assert r is not None                                        # X-01


def test_s3_tan_lacks_b5_38_but_alternative_exists():
    tan = one("SELECT * FROM workers WHERE id='W003'")
    assert tan["language"] == "vi"
    assert (D - date.fromisoformat(tan["hire_date"])).days == 90
    assert not has_training("W003", "B5-38")                    # H-11 반려
    assert has_training("W005", "B5-38")                        # 대안 제시


def test_s3_gas_below_10_percent():
    assert one("SELECT lel_percent FROM gas_readings WHERE zone_id='W2' ORDER BY ts DESC")["lel_percent"] < 10


# ── 변형용 구역 ──
def test_variant_zones():
    w1 = one("SELECT * FROM zones WHERE id='W1'")
    assert w1["routine_hot_work"] and w1["alarm_and_extinguisher"]   # H-01 예외
    assert dist("W1", "PB") > 11
    assert one("SELECT outdoor FROM zones WHERE id='YD'")["outdoor"] == 1


if __name__ == "__main__":
    fns = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for f in fns:
        f()
        print("ok", f.__name__)
    print(f"{len(fns)}개 통과")
