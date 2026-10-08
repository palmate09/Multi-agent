import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from graph.workflow import run_stub, run_team


def test_stub():
    out = run_stub("outputs/stub_test")
    assert len(out["nodes"]) == 7


def test_e2e():
    st = run_team("task manager", out_dir="outputs/e2e_test")
    assert st.status in ("accepted", "accepted_no_review", "tests_green"), st.status
