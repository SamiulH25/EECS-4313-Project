"""The agent: one model conversation that drives the testing tools from its shell.

The model decides what to do next (which tool to run, which unit to study, which tests and properties
to write, when to fix a crashing test, when to stop). The tools in student/tools do the mechanical and
slow parts (fuzzing against reference models, running properties on random inputs, the mutation-based
repair check, assembling tests_new/).

Two things keep the conversation inside the token budget: only the last few turns are sent verbatim,
while older turns become one line each in a progress log (their results live on in the tools' state,
which `survey` and `unit` report), and every observation carries a short status line with the tokens
left. Old turns are not resent as shortened commands because a small model then copies the shortened
form instead of writing real code.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path

from minisweagent.exceptions import FormatError, LimitsExceeded

KEEP_RECENT = 3          # turns sent verbatim; older ones are one line each in the progress log
LOG_LINES = 45           # progress-log lines sent
MAX_OBS = 4500           # characters of one tool output shown to the model
FINISH_AT = 40_000       # tokens left when the model is told to wrap up
STOP_AT = 12_000         # tokens left when the loop stops
MAX_STEPS = 140
AUTO_CONFIRM = 3         # repair-check slices the loop runs after a turn while failing tests are queued
SUBMIT = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


KEY_LINES = ("Ran ", "CONFIRMED", "rejected", "dropped", "Mined", "Fuzzer pass", "Wrote ", "The file cannot",
             "The file does not", "Not yet", "Fuzzing is complete", "Properties", "tests_new/ now", "Nothing is waiting")


def digest(cmd: str, out: str) -> str:
    """One line for the progress log: what a turn did and what came of it."""
    m = re.search(r"<<-?\s*'?(\w+)'?[^\n]*\n(.*?)\n\s*\1\s*(?:\n|$)", cmd, re.S)
    if m:
        f = re.search(r"cat\s*>+\s*(\S+)", cmd[:m.start()])
        names = re.findall(r"^\s*def ((?:test|prop)_\w+)", m.group(2), re.M)
        rest = " ".join(cmd[m.end():].split())
        what = f"wrote {f.group(1) if f else 'a file'} ({len(names)} tests/properties)" + (f"; {rest[:120]}" if rest else "")
    else:
        what = " ".join(cmd.split())[:160]
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    key = [ln for ln in lines if ln.startswith(KEY_LINES) or "is closed" in ln]
    if key:
        res = " / ".join(k[:140] for k in key[:6])
    elif lines:
        res = f"{len(lines)} lines of output"
    else:
        res = "no output"
    return f"{what} -> {res[:420]}"


TEXT_CALL = re.compile(r"<function=bash>\s*<parameter=command>\s*(.*?)\s*(?:</parameter>|</function>|</tool_call>|$)", re.S)


def textual_call(text: str) -> str:
    """A bash call the model wrote as text instead of a tool call (the last one in its reply)."""
    calls = TEXT_CALL.findall(text or "")
    return calls[-1].strip() if calls and calls[-1].strip() else ""


class AgentLoop:
    def __init__(self, task, model, env, system: str, instance: str):
        self.task, self.model, self.env = task, model, env
        self.system, self.instance = system, instance
        self.turns: list[dict] = []        # {"assistant": msg, "observations": [msg...]}
        self.log: list[dict] = []
        self.path = Path(task.trajectory_path) if task.trajectory_path else None
        self.started = time.time()

    @property
    def tokens_left(self) -> int:
        return getattr(self.model, "tokens_left", 10 ** 9)

    def messages(self) -> list[dict]:
        n = len(self.turns)
        older = self.turns[:max(0, n - KEEP_RECENT)]
        instance = self.instance
        if older:
            lines = [f"{i + 1}. {t['digest']}" for i, t in enumerate(older)][-LOG_LINES:]
            instance += ("\n\n## Your earlier steps (one line each; the tools keep their results, `survey` and "
                         "`unit <id>` show them; re-run a read if you need its text again)\n" + "\n".join(lines))
        msgs = [{"role": "system", "content": self.system}, {"role": "user", "content": instance}]
        for turn in self.turns[len(older):]:
            msgs.append({k: v for k, v in turn["assistant"].items() if k in ("role", "content", "tool_calls")})
            for o in turn["observations"]:
                msgs.append({k: v for k, v in o.items() if k != "extra"})
        return msgs

    def status(self) -> str:
        left = self.tokens_left
        line = f"[status: step {len(self.turns)}/{MAX_STEPS}, tokens left {left}]"
        if left < FINISH_AT:
            line += (" Budget almost used up: run `confirm` if tests are waiting, then `finalize`, then "
                     f"`echo {SUBMIT}`.")
        return line

    def execute(self, command: str) -> tuple[str, int, bool]:
        if SUBMIT in command and self.tokens_left > FINISH_AT:
            return (f"Not finished: {self.tokens_left} tokens are left, enough for many more tests. Keep testing: "
                    "run `survey` and pick a unit with few rounds, or finish the fuzz passes if any are left. "
                    f"Submit only when the status line says the budget is almost used up."), 1, False
        try:
            out = self.env.execute({"command": f"cd {shlex.quote(str(self.task.workspace))} && {command}"}, timeout=120)
        except Exception as e:  # noqa: BLE001 - Submitted and anything else from the environment
            if type(e).__name__ == "Submitted":
                return "", 0, True
            return f"{type(e).__name__}: {e}", -1, False
        text = out.get("output", "") or ""
        done = text.lstrip().startswith(SUBMIT) and out.get("returncode") == 0
        if out.get("exception_info"):
            text = f"{out['exception_info']}\n{text}"
        if len(text) > MAX_OBS:
            text = (text[:MAX_OBS // 2] + "\n... (output too long; use grep/head/sed for less) ...\n" +
                    text[-MAX_OBS // 2:])
        return text, out.get("returncode", -1), done

    def run(self):
        while len(self.turns) < MAX_STEPS and self.tokens_left > STOP_AT:
            try:
                msg = self.model.query(self.messages())
            except LimitsExceeded:
                break
            except FormatError as e:
                fix = (e.messages[0] if getattr(e, "messages", None) else
                       {"role": "user", "content": "Reply with one bash tool call."})
                text, finish = "", None
                try:
                    resp = fix.get("extra", {}).get("response") or {}
                    ch = (resp.get("choices") or [{}])[0] if isinstance(resp, dict) else {}
                    finish = ch.get("finish_reason")
                    m = ch.get("message") or {}
                    text = "\n".join(str(m.get(k) or "") for k in ("content", "reasoning_content", "reasoning"))
                except Exception:  # noqa: BLE001
                    pass
                cmd = textual_call(text)
                self.note("format-error", finish=finish, recovered=bool(cmd), content=text[-600:])
                if cmd:
                    # The model wrote its tool call as text (in its reasoning): run it as if it were a call.
                    out, code, done = self.execute(cmd)
                    self.note("step", command=cmd[:3000], output=out[:3000], returncode=code, textual=True)
                    obs = f"<returncode>{code}</returncode>\n<output>\n{out}\n</output>\n{self.status()}"
                    if not done:
                        extra = self.auto_confirm()
                        if extra:
                            obs += "\n[automatic repair check of queued failing tests]\n" + extra
                            out += f"\nauto-check: {extra.count('CONFIRMED')} confirmed, {extra.count('rejected')} rejected"
                    self.turns.append({"digest": digest(cmd, out),
                                       "assistant": {"role": "assistant", "content": f"I run:\n```bash\n{cmd}\n```"},
                                       "observations": [{"role": "user", "content": obs +
                                                         "\n(Use the bash tool call for your next command.)"}]})
                    if done:
                        break
                    continue
                self.turns.append({"digest": "(reply without a tool call)",
                                   "assistant": {"role": "assistant", "content": "(no tool call)"},
                                   "observations": [{"role": "user", "content": fix.get("content", "")}]})
                continue
            except Exception as e:  # noqa: BLE001 - network trouble: retry a few times
                self.note("model-error", error=f"{type(e).__name__}: {e}"[:300])
                time.sleep(3)
                if len([x for x in self.log[-4:] if x.get("tag") == "model-error"]) >= 4:
                    break
                continue
            actions = msg.get("extra", {}).get("actions") or []
            observations, finished = [], False
            digests = []
            for a in actions:
                text, code, done = self.execute(a.get("command", ""))
                finished = finished or done
                content = f"<returncode>{code}</returncode>\n<output>\n{text}\n</output>\n{self.status()}"
                observations.append({"role": "tool", "tool_call_id": a.get("tool_call_id"), "content": content})
                digests.append(digest(a.get("command", ""), text))
                self.note("step", command=a.get("command", "")[:3000], output=text[:3000], returncode=code)
                if finished:
                    break
            if observations and not finished:
                extra = self.auto_confirm()
                if extra:
                    observations[-1]["content"] += "\n[automatic repair check of queued failing tests]\n" + extra
                    digests[-1] += f" / auto-check: {extra.count('CONFIRMED')} confirmed, {extra.count('rejected')} rejected"
            self.turns.append({"digest": " | ".join(digests) or "(reply without a tool call)",
                               "assistant": msg, "observations": observations})
            if finished:
                break
        self.note("end", steps=len(self.turns), tokens_left=self.tokens_left,
                  minutes=round((time.time() - self.started) / 60, 1))

    def queued(self) -> int:
        try:
            state = json.loads((Path(self.task.workspace) / ".tagent" / "state.json").read_text())
            return len(state.get("pending") or [])
        except (OSError, ValueError):
            return 0

    def auto_confirm(self) -> str:
        """Run the slow repair check for the model (each slice is its own command under the limit)."""
        lines, last = [], []
        for _ in range(AUTO_CONFIRM):
            if not self.queued():
                break
            text, _, _ = self.execute("confirm")
            self.note("auto-confirm", output=text[:2000])
            lines += [ln for ln in text.splitlines() if ln.strip().startswith(("CONFIRMED", "rejected"))]
            last = text.strip().splitlines()[-2:] if text.strip() else []
        if not lines and not self.queued():
            return ""
        return "\n".join(lines[-25:] + (last if lines or self.queued() else []))

    def note(self, tag: str, **data):
        self.log.append({"tag": tag, "t": round(time.time() - self.started, 1),
                         "tokens_used_total": getattr(self.model, "total_tokens", None), **data})
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"steps": self.log}, indent=1))
            except OSError:
                pass
