from pathlib import Path

from testagent import bugs
from testagent.evaluate import source_access
from testagent.package import check_student
from testagent.projects import load_projects

SAMPLE = '''def clamp(value, low, high):
    """Clamp value to [low, high]. 5 is not a bug site."""
    if value < low and not high < low:
        return low
    if value > high or value == 7:
        return high - 0
    return value
'''


def test_candidates_cover_operators(tmp_path):
    path = tmp_path / "m.py"
    path.write_text(SAMPLE)
    found = bugs.candidates(path, "m.py")
    assert {b.operator for b in found} == {"comparison", "boolean", "remove_not", "arithmetic", "constant"}
    assert all(b.function == "clamp" for b in found)
    assert not any(b.original == "5" for b in found)


def test_each_candidate_applies_alone_and_together(tmp_path):
    path = tmp_path / "m.py"
    path.write_text(SAMPLE)
    found = bugs.candidates(path, "m.py")
    for bug in found:
        path.write_text(SAMPLE)
        bugs.apply(tmp_path, [bug])
        changed = path.read_text()
        assert changed != SAMPLE
        compile(changed, "m.py", "exec")
    spans, chosen = set(), []
    for bug in found:
        if (bug.start, bug.end) not in spans:
            spans.add((bug.start, bug.end))
            chosen.append(bug)
    path.write_text(SAMPLE)
    bugs.apply(tmp_path, chosen)
    compile(path.read_text(), "m.py", "exec")


def test_apply_refuses_a_mismatched_bug(tmp_path):
    path = tmp_path / "m.py"
    path.write_text(SAMPLE)
    bug = bugs.candidates(path, "m.py")[0]
    path.write_text("x = 1\n" + SAMPLE)
    try:
        bugs.apply(tmp_path, [bug])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_source_access_rejects_reading_the_code(tmp_path):
    cases = {"import os\n": "imports os", "import cachetools\nx = cachetools.__file__\n": "uses .__file__",
             "f = open('x')\n": "uses open", "from sys import argv\n": "imports sys"}
    for source, reason in cases.items():
        path = tmp_path / "test_x.py"
        path.write_text(source)
        assert source_access(path) == reason
    path.write_text("import cachetools\n\ndef test_a():\n    assert cachetools.LRUCache(2).maxsize == 2\n")
    assert source_access(path) == ""


def test_dev_projects_are_consistent():
    projects = load_projects()
    assert {p.name for p in projects} == {"cachetools", "sortedcontainers"}
    for project in projects:
        project_bugs = project.bugs()
        assert len(project_bugs) == 20
        assert len({b.id for b in project_bugs}) == 20
        for bug in project_bugs:
            data = (project.code / bug.file).read_bytes()
            assert data[bug.start:bug.end].decode() == bug.original


def test_starter_student_folder_is_valid():
    assert check_student(Path(__file__).resolve().parent.parent / "student") == []
