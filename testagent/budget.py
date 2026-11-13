"""The course model with a hard token budget.

Every model call your agent makes goes through BudgetModel. It counts prompt and completion tokens
and raises LimitsExceeded once the project's budget is used up, whatever loop you build around it.
"""

import threading

from minisweagent.exceptions import LimitsExceeded
from minisweagent.models import get_model


def response_usage(message: dict) -> tuple[int, int]:
    """Prompt and completion tokens recorded in a model message, or (0, 0)."""
    response = message.get("extra", {}).get("response") or {}
    usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if not usage:
        return 0, 0
    if not isinstance(usage, dict):
        usage = {"prompt_tokens": getattr(usage, "prompt_tokens", 0),
                 "completion_tokens": getattr(usage, "completion_tokens", 0)}
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


class BudgetModel:
    """Wraps the course model; use it exactly like a mini-swe-agent model."""

    def __init__(self, model, token_limit: int):
        self._model = model
        self._lock = threading.Lock()
        self.token_limit = token_limit
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.cost = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def tokens_left(self) -> int:
        return max(0, self.token_limit - self.total_tokens)

    def query(self, messages: list[dict], **kwargs) -> dict:
        if self.tokens_left <= 0:
            raise LimitsExceeded({"role": "exit", "content": "TokenLimitExceeded",
                                  "extra": {"exit_status": "TokenLimitExceeded", "submission": ""}})
        message = self._model.query(messages, **kwargs)
        prompt, completion = response_usage(message)
        with self._lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.calls += 1
            self.cost += message.get("extra", {}).get("cost", 0.0)
        return message

    def usage(self) -> dict:
        return {"model_calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "total_tokens": self.total_tokens,
                "token_limit": self.token_limit, "cost_usd": round(self.cost, 6)}

    def __getattr__(self, name):
        return getattr(self._model, name)


def build_model(settings: dict, token_limit: int) -> BudgetModel:
    """The fixed course model from settings, wrapped in a budget."""
    config = dict(settings["model"])
    config["observation_template"] = settings["observation_template"]
    config["format_error_template"] = settings["format_error_template"]
    return BudgetModel(get_model(config=config), token_limit)
