"""Test that every quote in POLICY_QUOTES appears word-for-word in the knowledge files."""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pytest
from tools.bar_tools import POLICY_QUOTES

# Load all knowledge files into one big string for lookup.
_KNOWLEDGE_DIR = os.path.join(_ROOT, "knowledge")
_KNOWLEDGE_TEXT = ""
for fname in os.listdir(_KNOWLEDGE_DIR):
    if fname.endswith(".txt"):
        with open(os.path.join(_KNOWLEDGE_DIR, fname), encoding="utf-8") as f:
            _KNOWLEDGE_TEXT += f.read() + "\n"


@pytest.mark.parametrize("section,quote", list(POLICY_QUOTES.items()))
def test_quote_appears_in_knowledge(section: str, quote: str) -> None:
    """Each quote must appear verbatim (substring match) in the knowledge files."""
    # Normalise whitespace for the search: collapse runs of whitespace to a
    # single space in both the knowledge text and the quote, then check.
    import re

    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    norm_knowledge = _norm(_KNOWLEDGE_TEXT)
    # The quote is a concatenation of phrases; check each sentence fragment
    # that was copied from the policy rather than the whole joined string.
    # We check the whole normalised quote as a substring.
    norm_quote = _norm(quote)
    assert norm_quote in norm_knowledge, (
        f"Quote for {section!r} not found verbatim in knowledge files.\n"
        f"Quote: {norm_quote!r}"
    )
