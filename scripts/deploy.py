"""Deploy maplewave_data connection, bar_tools, and spike_agent to Orchestrate."""

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


# ------------------------------------------------------------------
# 1. Connection
# ------------------------------------------------------------------
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
# 2. Tool
# ------------------------------------------------------------------
run([ORC, "tools", "import",
     "-k", "python",
     "-f", os.path.join(_ROOT, "tools", "bar_tools.py"),
     "-r", os.path.join(_ROOT, "tools", "requirements.txt"),
     "-a", APP_ID],
    "Import bar_tools")

# ------------------------------------------------------------------
# 3. Agent
# ------------------------------------------------------------------
run([ORC, "agents", "import",
     "-f", os.path.join(_ROOT, "agents", "spike_agent.yaml")],
    "Import spike_agent")

# ------------------------------------------------------------------
# 4. Deploy agent to live
# ------------------------------------------------------------------
run([ORC, "agents", "deploy", "-n", "maplewave_spike"],
    "Deploy spike_agent to live")

print("\n=== Deploy complete ===")
