"""GUI 스모크 테스트 (Streamlit AppTest, LLM 끔). S2를 신청부터 조건부 판정까지 화면 위젯으로 조작한다."""
import shutil
import sys
import tempfile
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


def make_app():
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.session_state["use_llm"] = False
    return at


def click(at, label):
    b = next(b for b in at.button if b.label == label)
    b.click().run()
    return at


def test_s2_flow_through_gui():
    at = make_app().run()
    assert not at.exception, at.exception
    click(at, "S2")
    assert at.session_state["text"].startswith("3호기")
    click(at, "1) 신청 구조화")
    assert not at.exception, at.exception
    labels = [r.label for r in at.radio]
    assert any("위험한계" in l for l in labels)
    # 되묻기 1라운드: 위험한계 진입 = 예, 6항목 = 6, 작업자 명단 = 없음
    next(r for r in at.radio if "위험한계" in r.label).set_value("예")
    next(n for n in at.number_input if "6항목" in n.label).set_value(6)
    assert any("함께 작업" in m.label for m in at.multiselect)
    at.button[[b.label for b in at.button].index("답변 반영")].click().run()
    # 2라운드: 안전블록
    next(r for r in at.radio if "안전블록" in r.label).set_value("예")
    at.button[[b.label for b in at.button].index("답변 반영")].click().run()
    assert not at.exception, at.exception
    click(at, "14항목 계획 수립")
    # '모름' 칸을 비해당으로 확정
    for sb in at.selectbox:
        if sb.label.startswith("판단 ") and sb.value == "모름":
            sb.set_value("비해당")
    at.button[[b.label for b in at.button].index("4) 검토 실행")].click().run()
    assert not at.exception, at.exception
    assert at.session_state["j"].status == "conditional"
    assert at.session_state["j"].by_id("M-02b").outcome == "conditional"


if __name__ == "__main__":
    db = ROOT / "data" / "permit_demo.db"
    backup = Path(tempfile.mkdtemp()) / "b.db"
    if db.exists():
        shutil.copy(db, backup)
    try:
        test_s2_flow_through_gui()
        print("ok test_s2_flow_through_gui\n1개 통과")
    finally:
        if backup.exists():
            shutil.copy(backup, db)
