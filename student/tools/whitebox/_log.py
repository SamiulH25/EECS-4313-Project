"""Live run log for probe, streamed to the developer's terminal.

Lines are appended to the file named by the PROBE_LOG environment variable, which
the local driver (scripts/final_run.py) sets and prints as lines arrive. Without
PROBE_LOG nothing is written, so graded runs leave no log behind and the model's
command output stays unchanged.
"""

import os
import time

ENV_VAR = "PROBE_LOG"


class Log:
    def __init__(self):
        self.lines = []
        self.started = time.monotonic()
        self.tag = "probe"
        self.path = os.environ.get(ENV_VAR)

    def __call__(self, text: str):
        stamp = f"{time.monotonic() - self.started:6.2f}s"
        for line in str(text).splitlines() or [""]:
            self.lines.append(f"[{self.tag} {stamp}] {line}")

    def flush(self):
        """Append buffered lines; safe to call before os.execv."""
        if not self.lines or not self.path:
            return
        try:
            with open(self.path, "a") as target:
                target.write("\n".join(self.lines) + "\n")
        except OSError:
            pass  # logging must never break test generation
        self.lines = []


def short(value, limit=160) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit - 20] + f"...<{len(text)} chars>"
