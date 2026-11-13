"""Projects under test and the workspace the agent works in."""

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from testagent import REPO_ROOT, bugs

DATA_DIR = REPO_ROOT / "data" / "dev"
NEW_TESTS = "tests_new"


@dataclass
class Project:
    """One project: correct code with the given tests in `code/`, and its bugs."""

    name: str
    package: str
    summary: str
    root: Path

    @property
    def code(self) -> Path:
        return self.root / "code"

    def bugs(self) -> list[bugs.Bug]:
        return bugs.load(self.root / "bugs.json")


def load_projects(folder: Path = DATA_DIR, only: list[str] | None = None) -> list[Project]:
    """Projects in a data folder, optionally only the named ones."""
    found = []
    for meta in sorted(Path(folder).glob("*/project.json")):
        data = json.loads(meta.read_text())
        if not only or data["name"] in only:
            found.append(Project(data["name"], data["package"], data["summary"], meta.parent))
    if only and len(found) != len(set(only)):
        missing = set(only) - {p.name for p in found}
        raise SystemExit(f"unknown project(s): {', '.join(sorted(missing))}")
    return found


def create_workspace(project: Project) -> Path:
    """A fresh folder with the project's code (bugs included), its given tests and an empty tests_new/."""
    workspace = Path(tempfile.mkdtemp(prefix=f"ws-{project.name}-")) / project.name
    shutil.copytree(project.code, workspace, ignore=shutil.ignore_patterns("__pycache__"))
    bugs.apply(workspace, project.bugs())
    (workspace / NEW_TESTS).mkdir()
    return workspace


def collect_tests(workspace: Path, target: Path) -> list[Path]:
    """Copy the Python files the agent wrote in tests_new/ to target."""
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    copied = []
    source = workspace / NEW_TESTS
    for path in sorted(source.rglob("*.py")) if source.is_dir() else []:
        if "__pycache__" in path.parts:
            continue
        dest = target / path.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, dest)
        copied.append(dest)
    return copied
