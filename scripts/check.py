"""Fast verification run by the Claude Code hook after edits.
Exit code 2 sends the message back to Claude as feedback. Keep it FAST. EDIT as needed."""

import importlib.util
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent


def run(cmd):
    result = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        out = (result.stdout + result.stderr).strip().splitlines()
        sys.stderr.write("\n".join(out[-40:]) + "\n")
        sys.exit(2)


if importlib.util.find_spec("ruff"):
    run([sys.executable, "-m", "ruff", "check", "."])

if (root / "tests").is_dir() and importlib.util.find_spec("pytest"):
    run([sys.executable, "-m", "pytest", "-q", "-x"])

sys.exit(0)
