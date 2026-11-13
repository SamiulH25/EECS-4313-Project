"""Single-turn model calls through the course `model`, with a log for the trajectory file."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from minisweagent.exceptions import FormatError, LimitsExceeded

HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n[ \t]*\2[ \t]*(?:\n|$)", re.S)


class LLM:
    def __init__(self, model, trajectory_path: Path | None):
        self.model = model
        self.path = Path(trajectory_path) if trajectory_path else None
        self.log: list[dict] = []
        self.lock = threading.Lock()

    @property
    def tokens_left(self) -> int:
        return getattr(self.model, "tokens_left", 10 ** 9)

    def ask(self, system: str, user: str, tag: str) -> str | None:
        """One fresh conversation. Returns the bash command the model sent, or None."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        started = time.time()
        command, content, error = None, "", ""
        for attempt in range(2):
            try:
                msg = self.model.query(messages)
                actions = msg.get("extra", {}).get("actions") or []
                content = msg.get("content") or ""
                if actions:
                    command = actions[0].get("command", "")
                break
            except LimitsExceeded:
                raise
            except FormatError as e:
                error = "format error (no tool call)"
                messages = messages + [{"role": "user", "content":
                                        "Your reply had no bash tool call. Reply again with exactly one bash tool call."}]
            except Exception as e:  # noqa: BLE001 - network errors etc.
                error = f"{type(e).__name__}: {e}"[:300]
                time.sleep(2)
        with self.lock:
            self.log.append({"tag": tag, "seconds": round(time.time() - started, 1), "prompt": user,
                             "content": content[:4000], "command": command, "error": error,
                             "tokens_used_total": getattr(self.model, "total_tokens", None)})
            self.save()
        return command

    def save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"calls": self.log}, indent=1))
        except OSError:
            pass

    def note(self, tag: str, **data):
        with self.lock:
            self.log.append({"tag": tag, **data})
            self.save()


def heredoc_body(command: str | None) -> str | None:
    if not command:
        return None
    m = HEREDOC.search(command + "\n")
    if m:
        return m.group(3)
    return None
