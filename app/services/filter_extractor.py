import json
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from app.models import FilterSchemaField
from app.services import llm_gateway


def _schema_description(schema: Dict[str, FilterSchemaField]) -> str:
    """Serialize the filter schema into a JSON-prompt-friendly description."""
    description: Dict[str, Any] = {}
    for name, field in schema.items():
        description[name] = field.model_dump(exclude_none=True)
    return json.dumps(description, indent=2, ensure_ascii=False, default=str)


def _format_history(history: List[Dict[str, str]]) -> str:
    lines = []
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        display_role = "User" if role == "user" else "Assistant"
        lines.append(f"{display_role}: {content}")
    return "\n".join(lines)


async def extract_filters(
    question: str,
    report_key: str,
    schema: Dict[str, FilterSchemaField],
    token_usage: Optional[List[Dict[str, int]]] = None,
    history: Optional[List[Dict[str, str]]] = None,
    previous_filters: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Use the cheap LLM to extract filters for this question and a list of the fields it set."""
    schema_json = _schema_description(schema)

    system = (
        "You are a filter extractor for an ERP chatbot. "
        "Extract structured filter values from the user's current question only. "
        "Return a JSON object with exactly two top-level keys: `filters` and `mentioned_fields`. "
        "`filters` contains only the filter values explicitly stated or resolved via pronouns in the current question. "
        "`mentioned_fields` is a list of the filter keys you set in `filters`. "
        "Do not ask the user for missing values; omit fields that are not mentioned. "
        "Use ISO dates (YYYY-MM-DD) for date fields and convert relative dates using today's date. "
        "If the user states an explicit year such as 2026, use exactly that year in start_date and end_date. "
        "Never substitute a different year such as 2024 or 2025 unless the user specifically asks for it. "
        "If the user mentions a sales rep name, a customer name, a customer code, a branch, a brand, or a metal type, set the matching filter field. "
        "Use previous filters and the previous conversation only to resolve pronouns such as 'she', 'he', 'it', or 'they'. "
        "Return only valid JSON with no markdown and no explanation."
    )

    user_prompt = (
        f"Report: {report_key}\n"
        f"Today's date: {date.today().isoformat()}\n"
        f"Filter schema:\n{schema_json}\n\n"
    )
    if previous_filters:
        user_prompt += f"Previous filters: {json.dumps(previous_filters, default=str)}\n\n"
    if history:
        user_prompt += f"Previous conversation:\n{_format_history(history)}\n\n"
    user_prompt += (
        f"User question: {question}\n\n"
        "Extracted filters and mentioned_fields as JSON:"
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]

    # Phase 3 escalation: try cheap, then strong, then hand off.
    try:
        raw = await llm_gateway.chat(
            tier="cheap",
            messages=messages,
            temperature=0.0,
            max_tokens=1024,
        )
        if token_usage is not None:
            token_usage.append(raw.usage)
        parsed = _parse_json(raw.text)
    except (FilterExtractError, llm_gateway.LLMGatewayError) as cheap_exc:
        try:
            raw = await llm_gateway.chat(
                tier="strong",
                messages=messages,
                temperature=0.0,
                max_tokens=1024,
            )
            if token_usage is not None:
                token_usage.append(raw.usage)
            parsed = _parse_json(raw.text)
        except Exception as strong_exc:
            raise FilterExtractError(
                f"Filter extraction failed after escalation: {strong_exc}. {llm_gateway.handoff_message()}"
            ) from strong_exc

    filters = parsed.get("filters", parsed) if isinstance(parsed, dict) else {}
    mentioned_fields = parsed.get("mentioned_fields", []) if isinstance(parsed, dict) else []
    if not isinstance(mentioned_fields, list):
        mentioned_fields = []

    # If the model omitted mentioned_fields, treat all non-empty returned filters as mentioned.
    if not mentioned_fields:
        mentioned_fields = [k for k, v in filters.items() if v is not None and v != ""]

    # Don't let the model invent date ranges when the user didn't ask for a date.
    _sanitize_dates(filters, mentioned_fields, question)

    return filters, mentioned_fields


def _parse_json(text: str) -> Dict[str, Any]:
    """Parse a JSON object from an LLM response, allowing markdown fences."""
    if text is None:
        raise FilterExtractError("LLM returned None content for filter extraction")
    text = text.strip()
    if not text:
        raise FilterExtractError("LLM returned empty content for filter extraction")

    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            text = match.group(1).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        # Last resort: grab the first {...} block.
        match = re.search(r"\{[\s\S]*?\}", text)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError as nested_exc:
                raise FilterExtractError(
                    f"Could not parse extracted filters as JSON: {nested_exc}\nRaw output: {text[:200]}"
                ) from nested_exc
        else:
            raise FilterExtractError(
                f"Could not parse extracted filters as JSON: {exc}\nRaw output: {text[:200]}"
            ) from exc

    if not isinstance(parsed, dict):
        raise FilterExtractError(
            f"Expected a JSON object for filters, got {type(parsed).__name__}\nRaw output: {text[:200]}"
        )
    return parsed


def _sanitize_dates(
    filters: Dict[str, Any],
    mentioned_fields: List[str],
    question: str,
) -> None:
    """Remove invented start/end dates when the question does not mention any date."""
    date_keywords = (
        "january|february|march|april|may|june|july|august|september|"
        "october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
    )
    question_lower = question.lower()
    has_date = (
        re.search(r"\b(?:" + date_keywords + r")\b", question_lower) is not None
        or re.search(r"\b20\d{2}\b", question_lower) is not None
        or re.search(
            r"\b(?:today|yesterday|tomorrow|now|current|last|this|next)\s+(?:week|month|year|quarter)|\b(?:week|month|year|quarter)\b",
            question_lower,
        )
        is not None
    )
    if not has_date:
        for key in ("start_date", "end_date"):
            filters.pop(key, None)
            if key in mentioned_fields:
                mentioned_fields.remove(key)


def merge_with_previous(
    previous: Optional[Dict[str, Any]],
    new: Dict[str, Any],
    mentioned_fields: List[str],
) -> Dict[str, Any]:
    """Merge newly extracted filters on top of the previous session filters."""
    result = dict(previous or {})
    for field in mentioned_fields:
        if field in new:
            result[field] = new[field]

    # If the user asks about a specific customer code, drop previous sales rep and date
    # filters that were not re-mentioned, because that usually means a new query.
    if "customer_code" in mentioned_fields:
        if "sales_rep" not in mentioned_fields:
            result.pop("sales_rep", None)
        if "start_date" not in mentioned_fields:
            result.pop("start_date", None)
        if "end_date" not in mentioned_fields:
            result.pop("end_date", None)

    # If a new sales rep is mentioned without a customer code, drop a previous
    # customer_code so the user is asking about the rep, not the old customer.
    if "sales_rep" in mentioned_fields and "customer_code" not in mentioned_fields:
        result.pop("customer_code", None)

    # Keep start/end dates together when either is explicitly mentioned.
    if "start_date" in mentioned_fields or "end_date" in mentioned_fields:
        if "start_date" in new:
            result["start_date"] = new["start_date"]
        if "end_date" in new:
            result["end_date"] = new["end_date"]

    return result


class FilterExtractError(Exception):
    """Raised when filter extraction fails."""
    pass
