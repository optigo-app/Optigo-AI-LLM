"""Prompt injection guard — sanitizes user input before it reaches LLM prompts."""

import re
from typing import Optional

from app.middleware.logging import get_logger

logger = get_logger(__name__)

_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?", re.IGNORECASE),
    re.compile(r"disregard\s+(?:all\s+)?(?:previous|prior)", re.IGNORECASE),
    re.compile(r"you\s+are\s+(?:now|actually)\s+(?:a|an)\s+", re.IGNORECASE),
    re.compile(r"forget\s+(?:everything|all\s+(?:previous|prior))", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"<\|system\|>", re.IGNORECASE),
    re.compile(r"\\n\\s*system\\s*:", re.IGNORECASE),
    re.compile(r"reveal\s+(?:your|the)\s+(?:system\s+)?prompt", re.IGNORECASE),
    re.compile(r"show\s+(?:me\s+)?(?:your|the)\s+(?:system\s+)?(?:prompt|instructions)", re.IGNORECASE),
    # SQL-specific injection through natural language
    re.compile(r"(?:drop|delete|truncate|alter|create)\s+(?:table|database|index|view)", re.IGNORECASE),
    re.compile(r"exec(?:ute)?\s+(?:xp_cmdshell|sp_|exec)", re.IGNORECASE),
    re.compile(r"\bxp_cmdshell\b", re.IGNORECASE),
    re.compile(r"\bsp_executesql\b", re.IGNORECASE),
    re.compile(r"union\s+(?:all\s+)?select", re.IGNORECASE),
    re.compile(r"(?:insert|update)\s+(?:into\s+)?(?:table|database)", re.IGNORECASE),
    # DAN / jailbreak patterns
    re.compile(r"do\s+anything\s+now", re.IGNORECASE),
    re.compile(r"you\s+are\s+(?:free|liberated|unrestricted)", re.IGNORECASE),
    re.compile(r"(?:jailbreak|jailbroken|jail\s+break)", re.IGNORECASE),
    re.compile(r"act\s+as\s+(?:if\s+you\s+(?:are|have)|a\s+(?:different|new))", re.IGNORECASE),
    # Encoding / obfuscation attempts
    re.compile(r"(?:base64|hex|url)\s+encode", re.IGNORECASE),
    re.compile(r"\\x[0-9a-f]{2}", re.IGNORECASE),
    # Prompt extraction attempts
    re.compile(r"(?:print|output|return|repeat|reveal)\s+(?:your|the)\s+(?:system\s+|initial\s+)?(?:prompt|message|instructions)", re.IGNORECASE),
    re.compile(r"what\s+(?:are|is)\s+your\s+(?:system\s+)?(?:prompt|instructions|rules)", re.IGNORECASE),
]

_MAX_QUESTION_LENGTH = 1000


def detect_injection(text: str) -> Optional[str]:
    """Return the matched pattern if injection is detected, else None."""
    if len(text) > _MAX_QUESTION_LENGTH * 3:
        return "input exceeds maximum allowed length"
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def sanitize_question(question: str) -> str:
    """Strip potential prompt-injection sequences from user question.

    This is a defense-in-depth layer. The question is also structurally
    separated from system prompts in the LLM messages, so even if something
    slips through, it appears as user content, not system instructions.
    """
    sanitized = question
    for pattern in _INJECTION_PATTERNS:
        sanitized = pattern.sub("[filtered]", sanitized)

    # Strip role-play markers
    sanitized = re.sub(r"<\|im_start\|>|<\|im_end\|>|<\|system\|>|<\|assistant\|>|<\|user\|>", "", sanitized)

    # Remove null bytes and control characters
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", sanitized)

    if sanitized != question:
        logger.warning("Prompt injection detected and sanitized in question")

    return sanitized
