"""Remove review_agent, intake_agent, bar_tools, maplewave_policies KB, and maplewave_data connection from Orchestrate."""

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

# Reverse order: agents → tools → knowledge base → connection
run([ORC, "agents", "remove", "-n", "maplewave_bar_review", "-k", "native"],
    "Remove agent maplewave_bar_review")
run([ORC, "agents", "remove", "-n", "maplewave_bar_intake", "-k", "native"],
    "Remove agent maplewave_bar_intake")
run([ORC, "agents", "remove", "-n", "maplewave_spike", "-k", "native"],
    "Remove agent maplewave_spike")

# Lookup tools
run([ORC, "tools", "remove", "-n", "get_today"],           "Remove tool get_today")
run([ORC, "tools", "remove", "-n", "find_customers"],      "Remove tool find_customers")
run([ORC, "tools", "remove", "-n", "get_contracts"],       "Remove tool get_contracts")
run([ORC, "tools", "remove", "-n", "get_invoice_lines"],   "Remove tool get_invoice_lines")
run([ORC, "tools", "remove", "-n", "get_outages"],         "Remove tool get_outages")
run([ORC, "tools", "remove", "-n", "get_request"],         "Remove tool get_request")
run([ORC, "tools", "remove", "-n", "list_queue"],          "Remove tool list_queue")
run([ORC, "tools", "remove", "-n", "get_past_decisions"],  "Remove tool get_past_decisions")

# Check tools
run([ORC, "tools", "remove", "-n", "check_rate_error"],    "Remove tool check_rate_error")
run([ORC, "tools", "remove", "-n", "check_sla_credit"],    "Remove tool check_sla_credit")
run([ORC, "tools", "remove", "-n", "check_promotion"],     "Remove tool check_promotion")
run([ORC, "tools", "remove", "-n", "check_duplicates"],    "Remove tool check_duplicates")
run([ORC, "tools", "remove", "-n", "check_etf"],           "Remove tool check_etf")
run([ORC, "tools", "remove", "-n", "check_completeness"],  "Remove tool check_completeness")
run([ORC, "tools", "remove", "-n", "approval_level"],      "Remove tool approval_level")

# Write / review tools
run([ORC, "tools", "remove", "-n", "submit_request"],      "Remove tool submit_request")
run([ORC, "tools", "remove", "-n", "record_decision"],     "Remove tool record_decision")
run([ORC, "tools", "remove", "-n", "review_request"],      "Remove tool review_request")

run([ORC, "knowledge-bases", "remove", "--name", "maplewave_policies"],
    "Remove knowledge base maplewave_policies")

run([ORC, "connections", "remove", "-a", "maplewave_data"],
    "Remove connection maplewave_data")

print("\n=== Remove complete ===")
