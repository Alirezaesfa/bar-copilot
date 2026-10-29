"""Remove spike_agent, bar_tools, and maplewave_data connection from Orchestrate."""

import os
import subprocess
import sys

# Use the venv orchestrate CLI so it works on Windows without PATH changes.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
ORC = os.path.join(_ROOT, "venv", "Scripts", "orchestrate")


def run(cmd: list[str], desc: str) -> None:
    """Run a CLI command; log but do not exit on failure (resource may not exist)."""
    print(f"\n>>> {desc}")
    print(" ".join(cmd))
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        print(f"  (exit {result.returncode}) — skipping, resource may not exist")


# Reverse order: agent → tool → connection
run([ORC, "agents", "remove", "-n", "maplewave_spike", "-k", "native"],
    "Remove agent maplewave_spike")

run([ORC, "tools", "remove", "-n", "get_customer_contracts"],
    "Remove tool get_customer_contracts")

run([ORC, "connections", "remove", "-a", "maplewave_data"],
    "Remove connection maplewave_data")

print("\n=== Remove complete ===")
