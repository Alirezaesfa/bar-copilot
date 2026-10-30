"""Run a set of tool calls locally using .env credentials."""

import datetime
import json
import os
import sys

# Add the project root to the path so this works from any CWD.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from tools.bar_tools import (
    check_rate_error,
    check_sla_credit,
    check_etf,
    list_queue,
)


def _unwrap(raw):
    """Unwrap ToolResponse if needed so we get plain JSON-serialisable data."""
    return raw.content if hasattr(raw, "content") else raw


def _show(label, raw):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print('='*60)
    result = _unwrap(raw)
    print(json.dumps(result, indent=2, default=str))


today = datetime.date.today().isoformat()

_show(
    "check_rate_error  K-20012  2026-01 to 2026-10  as_of today",
    check_rate_error("K-20012", "2026-01", "2026-10", today),
)

_show(
    "check_sla_credit  K-20022  2026-08  as_of 2026-10-15",
    check_sla_credit("K-20022", "2026-08", "2026-10-15"),
)

_show(
    "check_etf  K-20060  'customer is unhappy and wants the fee waived'",
    check_etf("K-20060", "customer is unhappy and wants the fee waived"),
)

_show(
    "check_sla_credit  K-20185  2026-10  as_of today",
    check_sla_credit("K-20185", "2026-10", today),
)

_show(
    "list_queue",
    list_queue(),
)
