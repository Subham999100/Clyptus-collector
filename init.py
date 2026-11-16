"""Environment + smoke check. Run at the start of every session. EDIT for your project."""

import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
print(f"Python: {sys.executable}")
if sys.prefix == sys.base_prefix:
    print("WARNING: virtual environment is not active (.venv).")

print("Checking installed packages...")
subprocess.run([sys.executable, "-m", "pip", "check"], cwd=root)

# TODO: add a real smoke test for your project, for example:
#   subprocess.run([sys.executable, "main.py", "--dry-run"], cwd=root, check=True)

print("Running scripts/check.py...")
sys.exit(subprocess.run([sys.executable, str(root / "scripts" / "check.py")]).returncode)
