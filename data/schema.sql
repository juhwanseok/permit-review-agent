-- 작업허가 검토 에이전트 데모 DB 스키마 v0.1 (SQLite)
-- 필드는 rules/permit_rules.yaml의 expr이 참조하는 것만 둔다 (Q7 시나리오에서 도출).
-- 시각은 ISO 8601 로컬시각 문자열('YYYY-MM-DDTHH:MM'), 불리언은 0/1.

PRAGMA foreign_keys = ON;
PRAGMA user_version = 2;   -- 스키마 버전: 바꾸면 seed.SCHEMA_VERSION도 함께 올린다

-- 사업장 (site.*)
CREATE TABLE site (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    psm_target          INTEGER NOT NULL DEFAULT 0,   -- PSM 대상 여부 (PSM-46 적용 판단)
    shift_start         TEXT NOT NULL,                -- 'HH:MM', C-02 허가 효력
    shift_end           TEXT NOT NULL
);

-- 현장 구역 = 배치도 (site_map.*, zone.*)
CREATE TABLE zones (
    id                      TEXT PRIMARY KEY,
    site_id                 TEXT NOT NULL REFERENCES site(id),
    name                    TEXT NOT NULL,
    x_m                     REAL NOT NULL,            -- 배치도 좌표(m), 11m 거리 계산용
    y_m                     REAL NOT NULL,
    location_type           TEXT NOT NULL DEFAULT '일반'
                            CHECK (location_type IN ('일반', '지하작업장', '가스도관굴착')),  -- H-07
    outdoor                 INTEGER NOT NULL DEFAULT 0,   -- C-06, C-07
    combustibles            INTEGER NOT NULL DEFAULT 0,   -- 상시 가연물 존재
    combustible_types       TEXT,                          -- 예: '도료,신너'
    routine_hot_work        INTEGER NOT NULL DEFAULT 0,   -- 같은 장소 상시·반복 용접 (H-01 단서)
    alarm_and_extinguisher  INTEGER NOT NULL DEFAULT 0    -- 경보설비 + 소화설비/소화기 (H-01 단서)
);

-- 설비 마스터 (equipment.*)
CREATE TABLE equipment (
    id                  TEXT PRIMARY KEY,
    site_id             TEXT NOT NULL REFERENCES site(id),
    name                TEXT NOT NULL,
    type                TEXT NOT NULL CHECK (type IN ('press', 'welder', 'conveyor', 'other')),
    zone_id             TEXT NOT NULL REFERENCES zones(id),
    power_kw            REAL,
    has_stored_energy   INTEGER NOT NULL DEFAULT 0,   -- M-03
    stored_energy_types TEXT,                          -- 예: '유압,중력(슬라이드)'
    -- ISO 20816-3 영역 경계 (비회전부 속도 rms, mm/s). 값은 B-M-37-2026 6.2(6) 범위 안에서 설비별로 정한다.
    zone_ab_limit       REAL,
    zone_bc_limit       REAL,
    zone_cd_limit       REAL,
    CHECK (zone_ab_limit IS NULL OR zone_ab_limit BETWEEN 0.71 AND 4.5),
    CHECK (zone_bc_limit IS NULL OR zone_bc_limit BETWEEN 1.8 AND 9.3),
    CHECK (zone_cd_limit IS NULL OR zone_cd_limit BETWEEN 4.5 AND 14.7)
);

-- 에너지 차단점과 현재 잠금·표지 상태 (loto_registry: p.lockable, p.locked, p.tagged)
CREATE TABLE isolation_points (
    id                  TEXT PRIMARY KEY,
    equipment_id        TEXT NOT NULL REFERENCES equipment(id),
    energy_type         TEXT NOT NULL,                -- 전기, 유압, 공압 ...
    description         TEXT,
    lockable            INTEGER NOT NULL,
    locked              INTEGER NOT NULL DEFAULT 0,
    tagged              INTEGER NOT NULL DEFAULT 0,
    key_holder          TEXT REFERENCES workers(id),
    updated_at          TEXT NOT NULL
);

-- 설비 단위 LOTO 확인 기록 (equipment.stored_energy_released, isolation_verified)
CREATE TABLE loto_checks (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id            TEXT NOT NULL REFERENCES equipment(id),
    stored_energy_released  INTEGER NOT NULL,         -- M-03
    isolation_verified      INTEGER NOT NULL,         -- M-04 (시동 시도 등)
    verify_method           TEXT,
    checked_by              TEXT REFERENCES workers(id),
    checked_at              TEXT NOT NULL
);

-- 절차서 (procedure.*): M-05 LOTO 절차서, C-03 작업 절차서
CREATE TABLE procedures (
    id              TEXT PRIMARY KEY,
    equipment_id    TEXT REFERENCES equipment(id),     -- NULL이면 사업장 공통
    kind            TEXT NOT NULL CHECK (kind IN ('LOTO', '작업')),
    task            TEXT,                              -- 작업 절차서의 대상 작업 (예: 금형교체)
    title           TEXT NOT NULL,
    revised_at      TEXT NOT NULL
);

-- 작업자 (w.*)
CREATE TABLE workers (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,
    language        TEXT NOT NULL DEFAULT 'ko',        -- 안내문 언어 (L.AX)
    hire_date       TEXT NOT NULL,
    can_sign_permit INTEGER NOT NULL DEFAULT 0         -- 허가권자 (DP-02)
);

-- 특별교육 이수 (w.has_special_training(code)). code = 시행규칙 별표5 제1호라목 번호
CREATE TABLE trainings (
    worker_id       TEXT NOT NULL REFERENCES workers(id),
    code            TEXT NOT NULL CHECK (code IN ('B5-2', 'B5-3', 'B5-11', 'B5-34', 'B5-38')),
    completed_at    TEXT NOT NULL,
    PRIMARY KEY (worker_id, code)
);

-- 센서 측정값 (equipment_sensor). data_source로 실제 기록 재생인지 합성인지 표시
CREATE TABLE sensor_readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id    TEXT NOT NULL REFERENCES equipment(id),
    ts              TEXT NOT NULL,
    current_a       REAL,                              -- 운전 여부 판단 (M-01)
    vib_rms         REAL,                              -- mm/s (M-09)
    data_source     TEXT NOT NULL DEFAULT 'synthetic'  -- 'synthetic' | 'KAMP:...' | 'AI4I' ...
);

-- 가스 측정 (gas.*)
CREATE TABLE gas_readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id         TEXT NOT NULL REFERENCES zones(id),
    ts              TEXT NOT NULL,
    lel_percent     REAL,
    o2              REAL,
    co2             REAL,
    co_ppm          REAL,
    h2s_ppm         REAL,
    measured_by     TEXT REFERENCES workers(id)
);

-- 허가 신청·허가 (permits_today, request.*)
CREATE TABLE permits (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL CHECK (type IN ('화기작업', '일반위험작업', '정전작업', '밀폐공간출입')),
    requester_id    TEXT NOT NULL REFERENCES workers(id),
    equipment_id    TEXT REFERENCES equipment(id),
    zone_id         TEXT NOT NULL REFERENCES zones(id),
    task            TEXT NOT NULL,
    start_ts        TEXT NOT NULL,
    end_ts          TEXT NOT NULL,
    handles_combustibles INTEGER NOT NULL DEFAULT 0,   -- X-01 동시작업 대조
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'reviewing', 'conditional', 'human_review',
                                      'approved', 'rejected', 'issued', 'suspended', 'closed')),
    request_text    TEXT,                              -- 원문 (자유 형식)
    request_lang    TEXT DEFAULT 'ko',
    request_json    TEXT,                              -- 구조화 결과 (request.* 필드)
    planning_json   TEXT,                              -- C-C-49 6.1 14항목 판단
    signed_by       TEXT REFERENCES workers(id),
    signed_at       TEXT
);

CREATE TABLE permit_workers (
    permit_id       TEXT NOT NULL REFERENCES permits(id),
    worker_id       TEXT NOT NULL REFERENCES workers(id),
    duty            TEXT NOT NULL DEFAULT '작업자',     -- 작업자 | 화재감시자 | 입회자
    PRIMARY KEY (permit_id, worker_id, duty)
);

-- 조건부 승인 증빙 (재검증 루프)
CREATE TABLE evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    permit_id       TEXT NOT NULL REFERENCES permits(id),
    rule_id         TEXT NOT NULL,
    image_path      TEXT,
    value_json      TEXT,                              -- 재측정값 등 수치 증빙
    vlm_result      TEXT,
    human_confirmed INTEGER,
    submitted_at    TEXT NOT NULL
);

-- 설비 이력 기억 (SP-01)
CREATE TABLE equipment_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id    TEXT NOT NULL REFERENCES equipment(id),
    ts              TEXT NOT NULL,
    event_type      TEXT NOT NULL,                     -- vib_zone_c, vib_zone_d, repair, permit_note ...
    detail          TEXT
);

-- 감사 로그
CREATE TABLE audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    permit_id       TEXT REFERENCES permits(id),
    ts              TEXT NOT NULL,
    step            TEXT NOT NULL,                     -- 구조화 | 계획 | 조회 | 판정 | 추론 | 증빙 | 서명 | 발행
    tool            TEXT,
    rule_id         TEXT,
    source_id       TEXT,
    result          TEXT,
    detail          TEXT
);

-- site.power_press_count (M-08: 동력 프레스 5대 이상 사업장)
CREATE VIEW site_stats AS
SELECT s.id AS site_id,
       (SELECT COUNT(*) FROM equipment e WHERE e.site_id = s.id AND e.type = 'press') AS power_press_count
FROM site s;

CREATE INDEX idx_sensor_eq_ts ON sensor_readings(equipment_id, ts);
CREATE INDEX idx_gas_zone_ts ON gas_readings(zone_id, ts);
CREATE INDEX idx_permits_time ON permits(start_ts, end_ts);
CREATE INDEX idx_events_eq_ts ON equipment_events(equipment_id, ts);
