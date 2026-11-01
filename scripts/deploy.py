"""Deploy maplewave_data connection, maplewave_policies KB, bar_tools, intake_agent, and review_agent to Orchestrate."""

import json
import os
import subprocess
import sys
from dotenv import load_dotenv

# Resolve the project root from the script's own location, not CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

load_dotenv(os.path.join(_ROOT, ".env"), override=True)

ORC = os.path.join(_ROOT, "venv", "Scripts", "orchestrate")
APP_ID = "maplewave_data"
KEYS = ["WXD_USER", "WXD_API_KEY", "PRESTO_HOST", "PRESTO_PORT",
        "PRESTO_CATALOG", "PRESTO_SCHEMA"]


def run(cmd: list[str], desc: str, redact: bool = False) -> None:
    """Run a CLI command and exit on failure."""
    print(f"\n>>> {desc}")
    if not redact:
        print(" ".join(cmd))
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        print(f"ERROR: '{desc}' failed (exit {result.returncode})")
        sys.exit(result.returncode)


def build_entries() -> list[str]:
    """Build -e flags for set-credentials without printing values."""
    entries = []
    for k in KEYS:
        val = os.environ.get(k, "")
        if not val:
            print(f"ERROR: {k} is not set in .env")
            sys.exit(1)
        entries += ["-e", f"{k}={val}"]
    return entries


def connection_exists(app_id: str) -> bool:
    """Return True if the connection already exists in the active env."""
    result = subprocess.run(
        [ORC, "connections", "list", "-v"],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(result.stdout)
        return any(c.get("app_id") == app_id for c in data)
    except (json.JSONDecodeError, TypeError):
        # Fall back to plain-text search if the output isn't JSON.
        return app_id in result.stdout


def kb_exists(name: str) -> bool:
    """Return True if the knowledge base already exists in the active env."""
    result = subprocess.run(
        [ORC, "knowledge-bases", "list"],
        capture_output=True, text=True,
    )
    return name in result.stdout


# ------------------------------------------------------------------
# 0. Login
# ------------------------------------------------------------------
api_key = os.environ.get("WXO_API_KEY", "")
if not api_key:
    print("ERROR: WXO_API_KEY is not set in .env")
    sys.exit(1)

run([ORC, "env", "activate", "wxo-trial", "-a", api_key],
    "Activate wxo-trial env", redact=True)

# ------------------------------------------------------------------
# 1. Connection
# ------------------------------------------------------------------
if connection_exists(APP_ID):
    print(f"\n>>> Register connection — already exists, skipping add")
else:
    run([ORC, "connections", "add", "-a", APP_ID],
        "Register connection")

for env in ("draft", "live"):
    run([ORC, "connections", "configure",
         "-a", APP_ID, "--env", env, "-t", "team", "-k", "key_value"],
        f"Configure connection ({env})")

for env in ("draft", "live"):
    entries = build_entries()
    run([ORC, "connections", "set-credentials",
         "-a", APP_ID, "--env", env] + entries,
        f"Set credentials ({env})", redact=True)

# ------------------------------------------------------------------
# 2. Knowledge base  (import upserts — idempotent)
# ------------------------------------------------------------------
KB_NAME = "maplewave_policies"
KB_FILE = os.path.join(_ROOT, "knowledge", "maplewave_policies.yaml")

if kb_exists(KB_NAME):
    print(f"\n>>> Import knowledge base — already exists, skipping")
else:
    run([ORC, "knowledge-bases", "import", "-f", KB_FILE],
        "Import maplewave_policies knowledge base")

# ------------------------------------------------------------------
# 3. Tool  (import upserts — no --safe means auto-update)
# ------------------------------------------------------------------
run([ORC, "tools", "import",
     "-k", "python",
     "-f", os.path.join(_ROOT, "tools", "bar_tools.py"),
     "-r", os.path.join(_ROOT, "tools", "requirements.txt"),
     "-a", APP_ID],
    "Import bar_tools")

# ------------------------------------------------------------------
# 4. Agents  (import upserts — no --safe means auto-update)
# ------------------------------------------------------------------
run([ORC, "agents", "import",
     "-f", os.path.join(_ROOT, "agents", "intake_agent.yaml")],
    "Import intake_agent")

run([ORC, "agents", "import",
     "-f", os.path.join(_ROOT, "agents", "review_agent.yaml")],
    "Import review_agent")

# ------------------------------------------------------------------
# 5. Deploy agents to live
# ------------------------------------------------------------------
run([ORC, "agents", "deploy", "-n", "maplewave_bar_intake"],
    "Deploy intake_agent to live")

run([ORC, "agents", "deploy", "-n", "maplewave_bar_review"],
    "Deploy review_agent to live")

print("\n=== Deploy complete ===")
