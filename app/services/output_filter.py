"""Output content filtering — validate LLM answers against source data to catch hallucinations."""

import re
from typing import Any, Dict, List

from app.middleware.logging import get_logger

logger = get_logger(__name__)

# Patterns that indicate the LLM is hedging or uncertain
_HEDGE_PATTERNS = [
    r"\b(I don't have|I do not have|I'm not sure|I cannot|I can't)\b",
    r"\b(might be|could be|possibly)\b",
    r"\b(as an AI|as a language model)\b",
]

# Patterns that indicate the LLM is leaking system prompt or instructions
_LEAK_PATTERNS = [
    r"\b(system prompt|instructions|you are a|your role)\b",
    r"\b(ignore previous|disregard the above)\b",
]


def extract_numbers(text: str) -> List[str]:
    """Extract all numeric values from text (including formatted numbers with commas)."""
    # Match numbers with optional commas/decimals: 1,234,567.89 or 42 or 3.14
    matches = re.findall(r'[\d,]+(?:\.\d+)?', text)
    # Filter out things that are clearly not financial figures (like "1" or "2" when they're list markers)
    result = []
    for m in matches:
        # Remove commas for comparison
        clean = m.replace(',', '')
        try:
            val = float(clean)
            # Only flag substantive numbers (not 0, 1, 2, etc. used as list markers)
            if val > 10 or '.' in m:
                result.append(m)
        except ValueError:
            pass
    return result


def extract_numbers_with_units(text: str) -> List[tuple]:
    """Extract numbers along with their crore/lakh multiplier from text.
    
    Returns list of (raw_string, numeric_value) where numeric_value accounts
    for crore (×1e7) or lakh (×1e5) suffixes.
    """
    # Match number followed by optional crore/lakh/thousand
    pattern = r'([\d,]+(?:\.\d+)?)\s*(crore|lakh|thousand|k|cr|lac)?'
    matches = re.findall(pattern, text, re.IGNORECASE)
    result = []
    for num_str, unit in matches:
        clean = num_str.replace(',', '')
        try:
            val = float(clean)
            if val <= 10 and '.' not in num_str:
                continue
            unit_lower = unit.lower().strip() if unit else ''
            if unit_lower in ('crore', 'cr'):
                val = val * 1e7
            elif unit_lower in ('lakh', 'lac'):
                val = val * 1e5
            elif unit_lower in ('thousand', 'k'):
                val = val * 1e3
            result.append((num_str, val))
        except ValueError:
            pass
    return result


def extract_numbers_from_data(data: Any) -> List[str]:
    """Extract all numeric values from the report data payload."""
    numbers = []
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, (int, float)):
                numbers.append(str(value))
            elif isinstance(value, str):
                # Try to parse as number
                try:
                    float(value.replace(',', ''))
                    numbers.append(value)
                except (ValueError, AttributeError):
                    pass
            elif isinstance(value, (list, dict)):
                numbers.extend(extract_numbers_from_data(value))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                numbers.extend(extract_numbers_from_data(item))
            elif isinstance(item, (int, float)):
                numbers.append(str(item))
    return numbers


def validate_answer(
    answer: str,
    data: Any,
    question: str = "",
) -> Dict[str, Any]:
    """Validate an LLM-generated answer against source data.

    Returns a dict with:
        - 'passed': bool — whether the answer passed all checks
        - 'warnings': List[str] — list of warning messages
        - 'unverified_numbers': List[str] — numbers in the answer not found in source data
    """
    warnings = []
    unverified = []

    # Check for hedge patterns (uncertainty)
    for pattern in _HEDGE_PATTERNS:
        if re.search(pattern, answer, re.IGNORECASE):
            warnings.append(f"LLM appears uncertain: matched pattern '{pattern}'")
            break

    # Check for system prompt leaks
    for pattern in _LEAK_PATTERNS:
        if re.search(pattern, answer, re.IGNORECASE):
            warnings.append(f"Possible system prompt leak: matched pattern '{pattern}'")
            break

    # Extract numbers from answer (with crore/lakh awareness) and check against source data
    answer_numbers_with_units = extract_numbers_with_units(answer)
    source_numbers_raw = extract_numbers_from_data(data)

    # Normalize source numbers for comparison (strip commas, compare as floats)
    source_floats = set()
    for n in source_numbers_raw:
        try:
            source_floats.add(round(float(n.replace(',', '')), 2))
        except (ValueError, AttributeError):
            pass

    for num_str, num_val in answer_numbers_with_units:
        try:
            num_float = round(num_val, 2)
            # Allow for rounding differences (±1% for large numbers, ±1 for small)
            tolerance = max(1.0, abs(num_float) * 0.001)
            found = any(abs(num_float - sf) <= tolerance for sf in source_floats)
            if not found:
                unverified.append(num_str)
        except (ValueError, AttributeError):
            pass

    if unverified:
        warnings.append(f"Answer contains {len(unverified)} number(s) not found in source data: {', '.join(unverified[:5])}")

    passed = len(warnings) == 0
    return {
        'passed': passed,
        'warnings': warnings,
        'unverified_numbers': unverified,
    }


def sanitize_answer(answer: str, validation: Dict[str, Any]) -> str:
    """Append a warning notice if the answer contains unverified figures."""
    if validation['passed']:
        return answer
    if validation['unverified_numbers']:
        return (
            answer
            + "\n\n⚠️ Note: Some figures in this answer could not be verified against the source data. "
            "Please cross-check with the report directly."
        )
    return answer
