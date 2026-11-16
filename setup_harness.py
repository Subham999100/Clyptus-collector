#!/usr/bin/env python3
"""setup_harness.py - scaffold a minimal Claude Code harness in the current project.

Works on Windows (PowerShell/CMD), macOS and Linux. No dependencies.
Based on the patterns in Anthropic's "Effective harnesses for long-running agents":
progress file + feature list + init script + git commits + verification.

Usage (from your project root, venv active):   python setup_harness.py
Safe to re-run: it never overwrites files that already exist.
"""

import datetime
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()


def write_if_missing(rel, content):
    path = ROOT / rel
    if path.exists():
        print(f"  skip   {rel} (already exists)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    print(f"  create {rel}")


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


CLAUDE_MD = """\
# Project harness rules

## Start of every session (do these first)
1. Run `pwd` and confirm you are in the project root.
2. Read `claude-progress.txt` and run `git log --oneline -20`.
3. Read `feature_list.json` and pick the highest-priority feature where `passes` is false.
4. Run `python init.py` for a quick environment/smoke check BEFORE new work.
   If it fails, fix that first.

## While working
- Work on ONE feature at a time. Do not try to build everything at once.
- Only change the `passes` field in `feature_list.json`. Never remove or edit
  feature descriptions or steps.
- Set `passes` to true ONLY after verifying the feature end to end by actually
  running the code with realistic input, not just by reading it.
- Keep the repo in a clean, mergeable state: no half-finished code, nothing broken.
- Use the project's virtual environment (.venv). Do not install packages globally.
- Never read or print secrets from .env files.

## End of every session
- Commit with a descriptive message.
- Append a short entry to `claude-progress.txt`: what was done, what is next,
  any known problems.

## Project commands (EDIT THESE)
- Install deps:  pip install -r requirements.txt
- Run:           <your run command, e.g. python main.py>
- Test:          pytest -q        (also run by scripts/check.py)
"""

PROGRESS = """\
# Progress log (newest entries at the bottom)

{today} - Harness created. No feature work yet.
Next: describe the project in feature_list.json, then start with the first feature.
"""

FEATURE_LIST = [
    {
        "id": 1,
        "category": "functional",
        "description": "REPLACE ME: one end-to-end behavior the collector should perform",
        "steps": [
            "Run the collector with a sample input",
            "Check the output / stored data",
            "Verify the expected result",
        ],
        "passes": False,
    }
]

INIT_PY = '''\
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
'''

CHECK_PY = '''\
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
        sys.stderr.write("\\n".join(out[-40:]) + "\\n")
        sys.exit(2)


if importlib.util.find_spec("ruff"):
    run([sys.executable, "-m", "ruff", "check", "."])

if (root / "tests").is_dir() and importlib.util.find_spec("pytest"):
    run([sys.executable, "-m", "pytest", "-q", "-x"])

sys.exit(0)
'''

START_SESSION = """\
Get your bearings, then start the next feature:
1. Run pwd, read claude-progress.txt, run `git log --oneline -20`.
2. Read feature_list.json and choose the highest-priority feature with passes=false.
3. Run `python init.py` and confirm it passes. Fix anything broken first.
4. Tell me which feature you will work on and your plan. Wait for my go-ahead.
"""

FINISH_SESSION = """\
Wrap up cleanly:
1. Verify the feature end to end by running it. Only then set passes=true.
2. Run `python scripts/check.py` and fix any failures.
3. Commit with a descriptive message.
4. Append a short entry to claude-progress.txt (done / next / known issues).
"""

GITIGNORE_ENTRIES = [
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    ".env",
    ".env.*",
    ".pytest_cache/",
    ".ruff_cache/",
]

SETTINGS = {
    "hooks": {
        "PostToolUse": [
            {
                "matcher": "Edit|Write|MultiEdit",
                "hooks": [{"type": "command", "command": "python scripts/check.py"}],
            }
        ]
    },
    "permissions": {"deny": ["Read(./.env)", "Read(./.env.*)"]},
}


def main():
    print(f"Setting up Claude Code harness in: {ROOT}")

    # 0. Preflight
    if shutil.which("git") is None:
        sys.exit("git is required. Install Git for Windows: https://git-scm.com/downloads/win")
    if shutil.which("claude"):
        print("  ok     claude CLI found")
    else:
        print(
            "  warn   claude CLI not found. In PowerShell run:  "
            "irm https://claude.ai/install.ps1 | iex"
        )
    if sys.prefix == sys.base_prefix:
        print("  warn   no virtual environment active")
    if "OneDrive" in str(ROOT):
        print(
            "  note   project is inside OneDrive; consider pausing sync or moving it (e.g. C:\\dev)"
        )
        print("         to avoid sync conflicts with .git and .venv")

    if not (ROOT / ".git").exists():
        git("init", "-q")
        print("  create git repository")

    # 1. .gitignore FIRST so .venv and .env are never committed
    gi = ROOT / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    missing = [e for e in GITIGNORE_ENTRIES if e not in existing]
    if missing:
        with gi.open("a", encoding="utf-8", newline="\n") as f:
            if existing and existing[-1] != "":
                f.write("\n")
            f.write("\n".join(missing) + "\n")
        print(f"  update .gitignore (+{len(missing)} entries)")

    # 2. Harness files
    write_if_missing("CLAUDE.md", CLAUDE_MD)
    write_if_missing(
        "claude-progress.txt", PROGRESS.format(today=datetime.date.today().isoformat())
    )
    write_if_missing("feature_list.json", json.dumps(FEATURE_LIST, indent=2) + "\n")
    write_if_missing("init.py", INIT_PY)
    write_if_missing("scripts/check.py", CHECK_PY)
    write_if_missing(".claude/commands/start-session.md", START_SESSION)
    write_if_missing(".claude/commands/finish-session.md", FINISH_SESSION)

    settings = ROOT / ".claude" / "settings.json"
    if settings.exists():
        print("  skip   .claude/settings.json (exists - merge this hook by hand):")
        print("         " + json.dumps(SETTINGS["hooks"]))
    else:
        write_if_missing(".claude/settings.json", json.dumps(SETTINGS, indent=2) + "\n")

    # 3. Initial commit
    git("add", "-A")
    result = git("commit", "-qm", "Add Claude Code harness scaffold")
    if result.returncode == 0:
        print("  commit initial harness")
    else:
        print("  note   no commit made (nothing new, or set git user.name / user.email first)")

    print(
        "\nDone. Next steps:\n"
        "  1. Edit CLAUDE.md (project commands), init.py and scripts/check.py for your project.\n"
        '  2. Describe your project as features in feature_list.json (all "passes": false).\n'
        "  3. Start Claude Code in this folder (venv active):   claude\n"
        "  4. Inside the session, run:   /start-session\n"
        "  5. Check what loaded:         /memory   and   /hooks"
    )


if __name__ == "__main__":
    main()
