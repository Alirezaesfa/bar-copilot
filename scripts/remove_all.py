"""Remove spike_agent, bar_tools, maplewave_policies KB, and maplewave_data connection from Orchestrate."""

import os
import subprocess
import sys
from dotenv import load_dotenv

# Use the venv orchestrate CLI so it works on Windows without PATH changes.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

load_dotenv(os.path.join(_ROOT, ".env"), override=True)

ORC = os.path.join(_ROOT, "venv", "Scripts", "orchestrate")


def run(cmd: list[str], desc: str, redact: bool = False) -> None:
    """Run a CLI command; log but do not exit on failure (resource may not exist)."""
    print(f"\n>>> {desc}")
    if not redact:
        print(" ".join(cmd))
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        print(f"  (exit {result.returncode}) — skipping, resource may not exist")


# ------------------------------------------------------------------
# 0. Login
# ------------------------------------------------------------------
api_key = os.environ.get("WXO_API_KEY", "")
if not api_key:
    print("ERROR: WXO_API_KEY is not set in .env")
    sys.exit(1)

run([ORC, "env", "activate", "wxo-trial", "-a", api_key],
    "Activate wxo-trial env", redact=True)

# Reverse order: agent → tool → knowledge base → connection
run([ORC, "agents", "remove", "-n", "maplewave_spike", "-k", "native"],
    "Remove agent maplewave_spike")

run([ORC, "tools", "remove", "-n", "get_customer_contracts"],
    "Remove tool get_customer_contracts")

run([ORC, "knowledge-bases", "remove", "--name", "maplewave_policies"],
    "Remove knowledge base maplewave_policies")

run([ORC, "connections", "remove", "-a", "maplewave_data"],
    "Remove connection maplewave_data")

print("\n=== Remove complete ===")
