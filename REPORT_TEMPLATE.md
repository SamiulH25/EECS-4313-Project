# Report: <team name>

Copy this file to `REPORT.md` and replace every placeholder in angle brackets. Keep it under about
1500 words, tables included. Every number must come from the evaluator's output on the development
projects.

## 1. Team

<names and student numbers>

## 2. Your agent

<describe what your agent does, step by step, and how its loop differs from the starter agent>

## 3. Testing techniques

At least two techniques from the lectures, implemented in the agent or the tools (not only in the prompt).

| Technique | Lecture topic | Where in the code | What it does |
|---|---|---|---|
| <name> | <topic> | <file and function> | <describe> |
| <name> | <topic> | <file and function> | <describe> |

## 4. Evidence that a technique finds new bugs

Paste the output of `python -m testagent.compare` for your agent with and without the technique, and
against the starter agent. Explain which bugs the technique found and why the other versions missed them.

<compare output and explanation>

## 5. Results on the development projects

| Run | Bugs found (of 40) | False alarms | Score | Tokens | Cost (USD) |
|---|---|---|---|---|---|
| Starter agent | <number> | <number> | <number> | <number> | <number> |
| Your final agent, run 1 | <number> | <number> | <number> | <number> | <number> |
| Your final agent, run 2 | <number> | <number> | <number> | <number> | <number> |

## 6. What you tried

At least three things you tried, including ones that did not help.

| Change | Bugs found | False alarms | Kept? |
|---|---|---|---|
| <describe> | <number> | <number> | <yes or no> |

## 7. Questions

1. How does your agent decide what an expected value should be, given that the code it can run is buggy?
2. How does your agent decide where to look in a project that does not fit in the model's context?
3. How does your agent keep false alarms down?
4. Why should your agent work on projects it has never seen? What did you do to avoid tuning it to the
   development projects?
5. Describe two bugs your agent still misses, and why, using its trajectories.
