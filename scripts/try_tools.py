"""Run get_customer_contracts locally using .env credentials."""

import json
import os
import sys

# Add the project root to the path so this works from any CWD.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from tools.bar_tools import get_customer_contracts

raw = get_customer_contracts("harbourline")

# When run locally the @tool decorator wraps the result in a ToolResponse.
# Unwrap it if needed so we can pretty-print plain JSON.
if hasattr(raw, "content"):
    result = raw.content
else:
    result = raw

print(json.dumps(result, indent=2, default=str))
print(f"\n{len(result)} contract(s) returned.")
