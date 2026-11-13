# SYSTEM
You review a disputed boundary condition in a Python library. Two versions of one comparison
differ only in whether the boundary value itself is included (`<` versus `<=`, or `>` versus `>=`).
Exactly one of them is the library's intended behaviour. Decide using the documentation, the
docstrings, the meaning of the names, and how the rest of the code treats the same boundary.
Do NOT assume the code shown is correct, and do not prefer a version because a test expects it.
Reply with exactly one bash tool call: `echo VERDICT: A`, `echo VERDICT: B` or `echo VERDICT: UNSURE`.

# USER
# Project `{{project}}`: {{summary}}

## Documentation excerpts
{{docs}}

## Code around the disputed comparison (line {{line}} of {{file}})
```python
{{context}}
```

## Other comparisons in this file that involve the same names
```python
{{siblings}}
```

## The two candidate versions of line {{line}}
Version A: `{{a}}`
Version B: `{{b}}`

{{question}}
If the documentation and the surrounding code do not settle it, answer UNSURE.
Reply with exactly one bash tool call: `echo VERDICT: A`, `echo VERDICT: B` or `echo VERDICT: UNSURE`.
