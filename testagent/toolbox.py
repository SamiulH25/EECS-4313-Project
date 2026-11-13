"""Discovery of the command line tools students give their agent."""

import os
import subprocess
import sys
from pathlib import Path

from testagent import REPO_ROOT

TOOLS_DIR = REPO_ROOT / "student" / "tools"
HELP_LINES = 25
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def discover(tools_dir: Path = TOOLS_DIR) -> list[Path]:
    """Executable tools in the tools folder."""
    if not tools_dir.is_dir():
        return []
    return [p for p in sorted(tools_dir.iterdir())
            if p.is_file() and not p.name.startswith((".", "_")) and p.suffix != ".md" and os.access(p, os.X_OK)]


def environment(tools_dir: Path = TOOLS_DIR) -> dict[str, str]:
    """Environment for agent commands: tools and this Python on PATH, every secret blanked."""
    folders = [str(tools_dir), str(Path(sys.executable).parent)]
    blanked = {name: "" for name in os.environ if any(marker in name.upper() for marker in SECRET_MARKERS)}
    return blanked | {
        "PATH": os.pathsep.join(folders + [os.environ.get("PATH", "")]),
        "PYTHONPATH": str(REPO_ROOT),
        "PAGER": "cat",
        "MANPAGER": "cat",
        "PIP_PROGRESS_BAR": "off",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def describe(workspace: Path, tools_dir: Path = TOOLS_DIR) -> str:
    """Markdown list of tools built from each tool's --help output."""
    env = os.environ | environment(tools_dir)
    sections = []
    for tool in discover(tools_dir):
        try:
            result = subprocess.run([str(tool), "--help"], cwd=workspace, env=env, capture_output=True,
                                    text=True, timeout=20)
            text = (result.stdout or result.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as error:
            text = f"(could not read --help: {error})"
        text = "\n".join(text.splitlines()[:HELP_LINES]) or "(no help text)"
        sections.append(f"### {tool.name}\n```\n{text}\n```")
    return "\n\n".join(sections) if sections else "(no tools available)"
