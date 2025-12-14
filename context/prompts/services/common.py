"""Shared utilities for prompt services."""

from __future__ import annotations

import logging
from typing import Any, List

import requests

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
LOG = logging.getLogger(__name__)


class PromptServiceError(Exception):
    """Raised when the prompt service cannot produce an answer."""


class PromptPendingAction(PromptServiceError):
    """Raised when an additional user interaction is required before executing."""

    def __init__(
        self,
        detail: str,
        *,
        command: dict,
        requirements: List[dict] | None = None,
        confirmation_message: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.pending_command = command
        self.requirements = requirements or []
        self.confirmation_message = confirmation_message


class PromptActionCancelled(PromptServiceError):
    """Raised when the user declines to continue with the pending action."""


def extract_error_detail(response: requests.Response) -> str:
    """Return the most relevant error detail from a Groq response."""

    try:
        payload: Any = response.json()
    except ValueError:
        text = response.text.strip()
        return text or "Respuesta vacia."

    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    if isinstance(error, list):
        return "; ".join(str(item) for item in error)
    if error:
        return str(error)
    return str(payload)
