"""CLI: --stub, --run, --eval, --live, ablations (--no-tester/--no-reviewer/--no-reasoner)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub", action="store_true")
    ap.add_argument("--run", type=str, default="")
    ap.add_argument("--out", type=str, default="outputs/demo")
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--no-tester", action="store_true")
    ap.add_argument("--no-reviewer", action="store_true")
    ap.add_argument("--no-reasoner", action="store_true")
    ap.add_argument("--docker", action="store_true")
    args = ap.parse_args()

    if args.stub:
        from graph.workflow import run_stub
        print(json.dumps(run_stub(), indent=2))
        return
    if args.eval:
        from evals.metrics import summarize_run
        from graph.workflow import run_team
        suite = json.loads((BACKEND / "evals" / "requirements_suite.json").read_text())
        rows = []
        for item in suite:
            d = f"outputs/eval_{item['id']}"
            run_team(item["requirement"], out_dir=d, verbose=args.live,
                     skip_tester=args.no_tester, skip_reviewer=args.no_reviewer,
                     skip_reasoner=args.no_reasoner)
            rows.append(summarize_run(d))
        Path("outputs").mkdir(exist_ok=True)
        (BACKEND / "evals" / "results.json").write_text(json.dumps(rows, indent=2))
        print(json.dumps(rows, indent=2))
        return
    req = args.run or "Build a REST API for managing tasks with SQLite persistence."
    from graph.workflow import run_team
    st = run_team(req, out_dir=args.out, verbose=True,
                  skip_tester=args.no_tester, skip_reviewer=args.no_reviewer,
                  skip_reasoner=args.no_reasoner,
                  use_docker=args.docker)
    print(f"status={st.status} dev_retries={st.retry_dev} tests={st.report.passed if st.report else 0} pass")

if __name__ == "__main__":
    main()
