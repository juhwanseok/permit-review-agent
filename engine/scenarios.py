"""Q7 시연 시나리오 S1~S3의 입력.

request와 planning은 원래 LLM이 만드는 부분(신청 구조화, 14항목 계획)이다.
판정 엔진을 LLM 없이 검증하려고 정답 형태로 고정해 둔다.
"""
DEMO_DATE = "2026-10-05"
D = DEMO_DATE


def plan(haedang=(), reasons=None):
    """14항목 계획. haedang에 든 번호만 '해당', 나머지는 '비해당'."""
    reasons = reasons or {}
    return {str(i): {"answer": "해당" if i in haedang else "비해당", "reason": reasons.get(i, "")}
            for i in range(1, 15)}


S1 = {
    "now": f"{D}T10:30",
    "request": {
        "permit_id": f"S1-{D}", "work_type": "maintenance", "requester": "W001", "workers": ["W001"],
        "equipment_id": "P-05", "zone_id": "L1", "task": "점검",
        "start": f"{D}T10:30", "end": f"{D}T11:30",
        "request_text": "5호기에서 소리가 좀 이상해서 봐야 할 것 같아요. 오전 중에요.",
        "body_in_danger_zone": False,
    },
    "planning": plan({2, 7, 8}, {2: "전동기 정비 전 주전원 차단", 7: "유압 라인 잔압", 8: "유압 유닛 내부 잔류"}),
    "expected": "approved",
}

S2 = {
    "now": f"{D}T13:00",
    "request": {
        "permit_id": f"S2-{D}", "work_type": "maintenance", "requester": "W001", "workers": ["W001"],
        "equipment_id": "P-03", "zone_id": "L1", "task": "금형교체",
        "start": f"{D}T13:00", "end": f"{D}T15:00",
        "request_text": "3호기 금형 교체합니다. 13시부터 15시까지.",
        "body_in_danger_zone": True, "safety_block_used": True, "die_precheck_completed": 6,
    },
    "planning": plan({2, 7, 8}),
    "expected": "conditional",
    "evidence": {"rule_id": "M-02b", "point": "P-03-E", "at": f"{D}T13:10", "recheck_at": f"{D}T13:12",
                 "verify_at": f"{D}T13:14", "final_at": f"{D}T13:15",
                 "verify_method": "잠금 후 기동 스위치 시동 시도 → 무반응",
                 "image_path": "evidence/S2_P-03-E_lock.jpg",
                 "vlm_result": "차단기 손잡이에 자물쇠 체결 확인, 꼬리표 이름 '김정비'"},
    "expected_after_evidence": "conditional",       # 잠금이 바뀌었으므로 차단 확인(M-04)을 다시 해야 함
    "expected_after_verify": "human_review",        # M-09 진동 C영역은 허가권자가 판단
}

S3 = {
    "now": f"{D}T13:50",
    "request": {
        "permit_id": f"S3-{D}", "work_type": "hot_work", "requester": "W003", "workers": ["W003"],
        "fire_watch": None, "equipment_id": "WD-01", "zone_id": "W2", "task": "용접",
        "start": f"{D}T14:00", "end": f"{D}T16:00",
        "request_text": "Tôi sẽ hàn giá đỡ ở dây chuyền 2. Xung quanh không có vật dễ cháy. 14h–16h.",
        "request_lang": "vi",
        "extinguisher_placed": True, "spark_barrier": "인증방화포", "ventilation_done": True,
        "excess_combustibles_removed": True, "post_work_fire_watch_min": 30,
        "claimed_no_combustibles": True,
    },
    "planning": plan({6, 9, 10, 11, 12}, {6: "인접 도장 부스 인화성 증기 가능", 12: "화재감시자 필요 여부 확인"}),
    "expected": "rejected",
}

ALL = {"S1": S1, "S2": S2, "S3": S3}
