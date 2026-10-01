# 작업허가 검토 에이전트 (Permit Review Agent)

제4회 경남 AI·SW 경진대회 · 대학부 지정과제 3-3 (국립창원대 앵커사업단) 출품작. 개발 기간 2026-09-29 ~ 10-06.

> **한계와 법적 근거가 없는 값은 [LIMITATIONS.md](LIMITATIONS.md)에 모았다.** 보고서와 별지2에 한계를 적을 때 이 문서를 원본으로 쓴다.

## GUI 실행
```
.venv/Scripts/streamlit run app.py          # http://localhost:8501
```
- 로컬 LLM: Ollama + `gemma4:31b` (24GB급 GPU 필요, L-C07). 연결되지 않으면 사이드바에 폴백 모드로 표시되고, 판정 엔진은 그대로 동작한다.
- 사이드바의 **S1·S2·S3** 버튼으로 시연 시나리오를 불러온다. 데모 시계·체감온도·특보는 수동 입력이다(L-D03).
- 흐름: ① 신청(구조화 → 되묻기 → 14항목) → ② 검토·조치(판정·모순·대안·증빙·재검증) → ③ 서명·발행(허가서·게시문·다국어 안내) → ④ 작업 감시(이벤트 주입 → 정지) → ⑤ 기록 → ⑥ 규칙·한계
- 앱이 시작되면 백그라운드에서 모델을 미리 로드한다(워밍업). 로드 전 첫 호출은 약 17초, 로드 후 짧은 호출은 약 2.7초다.

정비작업·화기작업 허가 신청을 검토하는 AI 에이전트입니다. 역할 분담은 "LLM은 계획·해석, 규칙은 판정, 사람은 서명"입니다.

## 구성
| 경로 | 내용 |
|---|---|
| `rules/permit_rules.yaml` | 판정 규칙 31개. 출처 21개(법령·고시·KOSHA GUIDE·행정지침). **이 파일이 정본** |
| `rules/validate_rules.py` | 규칙 파일 무결성 검사. 출처 누락, 등급 상한 초과, 조항 오기를 잡음 |
| `data/schema.sql` | 데모 DB 스키마(SQLite, 테이블 15개 + 뷰 1개) |
| `data/seed.py` | 가상 사업장 '가온정밀'의 하루치 상태를 생성 (`--date`로 기준일 지정) |
| `tests/test_validate_rules.py` | 검사기 음성 테스트 8개 |
| `tests/test_seed.py` | 시드 DB가 시나리오 S1~S3 전제를 담고 있는지 검사하는 테스트 14개 |
| `engine/tools.py` | 상태 조회 도구. 호출마다 실행 로그에 기록. 현장 연동 시 이 파일만 교체 |
| `engine/checks.py` | 규칙 31개의 판정 함수 (YAML의 expr을 구현, 규칙 ID와 1:1 대응) |
| `engine/judge.py` | 판정 → 집계 → LLM 강화 조정 제한 → 증빙 재검증 → 허가권자 서명 → 감사 로그·기억 |
| `engine/scenarios.py` | S1~S3 입력 (LLM이 만들 구조화 결과·14항목 계획을 정답 형태로 고정) |
| `engine/demo.py` | 시나리오 실행 로그 출력 |
| `tests/test_engine.py` | 엔진 테스트 23개 (시나리오 3건 + 변형 + DP-01~03) |
| `engine/llm.py` | Gemma 4 연동: 구조화, 14항목(하이브리드), 모순 탐지, 번역, 사진 판독. 폴백 포함 |
| `engine/pipeline.py` | 전체 흐름: 되묻기, 증빙 처리기 14종, 대안, 문서(허가서·게시문·안내), 작업 감시 |
| `app.py` | Streamlit GUI (6단계) |
| `assets/make_samples.py` | 합성 증빙 사진 생성 (L-D05) |
| `tests/test_pipeline.py` | 파이프라인 테스트 10개 (LLM 없이 폴백 모드) |
| `tests/test_hardening.py` | 견고성 테스트 16개 (재현한 버그 A1~A9·B1~B3, 작업자 명단, 키워드 교차 확인) |
| `tests/test_app.py` | GUI 스모크 테스트 1개 (Streamlit AppTest로 S2 조작) |
| `tests/eval_cases.py`, `tests/run_eval.py` | 정확도 평가셋(개발 24건 + 보류 10건)과 실행기 |
| `tests/eval_out/` | 평가 결과. `gemma_holdout_first`가 보류셋 첫 실행(수정 전) 결과다 |
| `tests/llm_smoke.py` | Gemma 4 실모델 점검 (수동 실행, 느림) |
| `LIMITATIONS.md` | 한계·법적 근거 없는 값 목록 (L-A/B/C/D/S/P) |

## 실행
```
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
python data/seed.py --date 2026-10-05
python -m engine.demo all            # S1·S2·S3 실행 로그
python tests/test_engine.py
python rules/validate_rules.py
python tests/test_validate_rules.py
python tests/test_seed.py
python tests/test_pipeline.py
python tests/test_hardening.py
python tests/test_app.py

# 정확도 평가 (llm은 Ollama gemma4:31b 필요)
python tests/run_eval.py fallback tests/eval_out/fallback_dev dev
python tests/run_eval.py llm tests/eval_out/gemma_holdout holdout
```

## 평가 결과 요약
| | Gemma 4 보류 10건 (첫 실행) | Gemma 4 34건 (수정 후) | 폴백(규칙만) 34건 |
|---|---|---|---|
| 구조화 필드 정확도 | 96.6% (170/176) | 99.5% (595/598) | 85.6% (512/598) |
| 최종 판정 일치 | 7/10 | 34/34 | 34/34 |
| **오승인** | **0** | **0** | **0** |

- 정답 판정은 사람이 확정한 입력으로 규칙 엔진을 돌린 결과다. 즉 **규칙 엔진이 옳다는 전제의 일치율**이다.
- 수정 후 수치는 평가셋을 보고 고친 결과이므로 일반화 성능으로 보지 않는다. 한계는 LIMITATIONS.md L-C12.

## 데이터 출처 구분 (보고서·별지2에 그대로 옮길 것)
- **가상:** 사업장, 구역 배치도, 설비 마스터, LOTO 상태, 절차서, 작업자, 교육 이력, 가스 측정, 당일 허가
- **합성 → 교체 예정:** 센서값(`sensor_readings.data_source='synthetic'`). KAMP나 AI4I 실제 기록 재생으로 교체
- **실제 API:** 기상청 단기예보 (엔진 구현 시 연결)

## 시드 상태 요약 (시나리오 대응)
| 시나리오 | 준비된 상태 |
|---|---|
| S1 5호기 점검 → 승인 | 10:00 정지(전류 0), 마지막 진동 2.1mm/s(B영역). 차단점 2개 잠금. 잔압 방출·차단 확인 기록, LOTO 절차서, 김정비 별표5 11호 이수 |
| S2 3호기 금형교체 → 조건부 | 주전원 **표지만**(잠금 가능), 유압 잠금. 진동 6.2mm/s(**C영역**, 경계 4.5/7.1). 12일 전 C영역 이력 1회 |
| S3 2라인 용접 → 반려 | W-2에서 7m 거리에 도장 부스(도료·신너). 13~17시 도장 허가 발급됨. Tan 38호 **미이수**. 대안 인력 최용접 38호 이수. W-2 LEL 3% |
| 변형용 | W1 용접 전용 부스(H-01 예외 조건 충족), YD 옥외(폭염·악천후) |

## 스키마 결정 메모
- **진동 경계값은 설비별 컬럼으로 두었다.** 입력 범위는 B-M-37-2026 6.2(6)으로 CHECK 제약을 걸었다. 값은 ISO 20816-3 Group 2 유연지지(2.3/4.5/7.1)이다.
- **H-01 단서(상시·반복 용접 + 경보·소화설비)는 구역 단위로 둔다.** 조문 문구가 "같은 장소에서"이기 때문이다. 그래서 규칙 expr을 `site.*`에서 `zone.*`으로 바꿨다.
- **동력 프레스 대수는 `site_stats` 뷰로 계산한다.** 설비 테이블과 값이 어긋나지 않게 하려는 것이다.
- **`permits.request_json`에는 LLM이 구조화한 신청 필드를 저장한다.** 예: fire_watch_assigned, safety_block_used.

## 판정 엔진 결정 메모
- **판정 규칙:** 비적용이면 `n/a`, 데이터가 없으면 `human_review`(DP-03), 통과면 `pass`, 불통과면 YAML의 `on_fail`.
- **집계:** 가장 강한 결과를 따른다. reject → 반려, conditional → 조건부, human_review → 사람 판단, 나머지 → 승인(서명 대기).
- **LLM 조정:** 강화만 가능하고 상한은 human_review다(설계 등급). 완화나 반려를 시도하면 예외가 발생한다.
- **서명 조건:** 조건부 항목이 남아 있으면 서명할 수 없다. 사람 판단 항목은 전부 결정해야 서명할 수 있다. 서명 권한은 `can_sign_permit`이 있는 사람만 가진다.
- **기억 기록 시점:** 진동 C/D 영역 이력은 서명할 때 한 번만 쓴다. 재판정할 때마다 쓰면 중복으로 쌓이기 때문이다.
- **신청 필드 처리:** 필드가 빠져 있으면 `missing_fields`에 모은다. 이 목록이 LLM 되묻기의 입력이 된다. 필드가 있지만 값이 null이면 '없음'으로 판정한다(예: 화재감시자 미지정 → 반려).
- **아직 못 잡는 것:** 신청자가 사실과 다르게 적은 값(예: S3 "가연물 없음", 가연물 격리 완료)은 H-06처럼 신청값을 믿고 통과시킨다. 이런 모순은 LLM 추론 단계가 도구 결과와 대조해 human_review로 올려야 한다.
