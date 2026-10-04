"""Run all checks in a disposable copy, keeping the user's demo database unchanged."""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ["test_validate_rules", "test_seed", "test_engine", "test_pipeline", "test_hardening", "test_app", "test_submission"]


def main():
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    results = []
    with tempfile.TemporaryDirectory(prefix="permit_checks_") as tmp:
        copy = Path(tmp) / "agent"
        shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", "permit_demo.db", "evidence"))
        commands = [["rules/validate_rules.py"], *[[f"tests/{t}.py"] for t in TESTS], ["-m", "engine.demo", "all"]]
        for command in commands:
            print("\nRUN", " ".join(command), flush=True)
            result = subprocess.run([sys.executable, "-B", *command], cwd=copy, env=env)
            results.append({"command": command, "exit_code": result.returncode})
    count = sum(sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
                    for n in ast.parse((ROOT / "tests" / f"{t}.py").read_text(encoding="utf-8")).body) for t in TESTS)
    failed = any(r["exit_code"] for r in results)
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(json.dumps({"test_count": count, "passed": not failed, "commands": results},
                                                ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'SOME TESTS FAILED' if failed else 'ALL PASSED'} ({count} tests)")
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
