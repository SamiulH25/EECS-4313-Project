"""List the bugs one run found and another did not.

Usage:
    python -m testagent.compare --run with-technique --against without-technique

Both runs must be evaluated first. Use this to show that a technique finds bugs the other agent missed.
"""

import argparse
import json

from testagent.run import RUNS_DIR


def found_by(run: str) -> dict[str, set[str]]:
    path = RUNS_DIR / run / "evaluation.json"
    if not path.exists():
        raise SystemExit(f"missing {path}: run `python -m testagent.evaluate --run {run}` first")
    return {p["project"]: set(p["found"]) for p in json.loads(path.read_text())["projects"]}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True)
    parser.add_argument("--against", required=True)
    args = parser.parse_args(argv)
    ours, theirs = found_by(args.run), found_by(args.against)
    only_ours = only_theirs = 0
    for project in sorted(set(ours) | set(theirs)):
        a, b = ours.get(project, set()), theirs.get(project, set())
        only_ours += len(a - b)
        only_theirs += len(b - a)
        print(f"{project}: {args.run} {len(a)}, {args.against} {len(b)}, both {len(a & b)}")
        for bug in sorted(a - b):
            print(f"  only {args.run}: {bug}")
        for bug in sorted(b - a):
            print(f"  only {args.against}: {bug}")
    print(f"Total: {only_ours} bugs only {args.run} found, {only_theirs} only {args.against} found.")


if __name__ == "__main__":
    main()
