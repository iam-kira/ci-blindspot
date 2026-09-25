"""Tests for ci_blindspot.

Run with ``pytest``. The bundled ``python ci_blindspot.py --self-check`` is also
exercised here so the two never drift apart.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ci_blindspot import (
    analyse,
    ci_platforms,
    links_in_tree,
    render,
    render_survey,
    self_check,
    tracked_symlinks,
)


def _repo(tmp_path: Path, workflow: str | None = None, files: dict[str, str] | None = None) -> Path:
    if workflow is not None:
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "test.yml").write_text(workflow, encoding="utf-8")
    for name, content in (files or {}).items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


def test_self_check_passes():
    assert self_check() == 0


def test_ci_matrix_detected(tmp_path):
    repo = _repo(tmp_path, workflow="jobs:\n  a:\n    runs-on: ubuntu-latest\n  b:\n    runs-on: windows-latest\n")
    platforms, count = ci_platforms(repo)
    assert platforms == {"linux", "windows"}
    assert count == 1
    assert analyse(repo).uncovered == ["macos"]


def test_no_workflows():
    # A repo with no .github/workflows reports zero platforms, not a crash.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        platforms, count = ci_platforms(Path(tmp))
        assert platforms == set()
        assert count == 0


def test_unguarded_symlink_flagged(tmp_path):
    repo = _repo(tmp_path, files={"code.py": "from pathlib import Path\ndef f(p):\n    (p / 'a').symlink_to(p)\n"})
    report = analyse(repo)
    calls = [f for f in report.findings if f.kind == "privileged-call"]
    assert len(calls) == 1
    assert report.guarded == 0


def test_try_except_oserror_is_guarded(tmp_path):
    code = "import os\ndef f(p):\n    try:\n        os.symlink(p, p)\n    except OSError:\n        pass\n"
    report = analyse(_repo(tmp_path, files={"code.py": code}))
    assert not report.findings
    assert report.guarded == 1


def test_suppress_oserror_is_guarded(tmp_path):
    code = "from contextlib import suppress\ndef f(p):\n    with suppress(OSError):\n        p.symlink_to(p)\n"
    report = analyse(_repo(tmp_path, files={"code.py": code}))
    assert not report.findings
    assert report.guarded == 1


def test_narrow_suppress_is_still_flagged(tmp_path):
    # suppress(FileExistsError) does NOT cover WinError 1314 (the pipx bug).
    code = "from contextlib import suppress\ndef f(p):\n    with suppress(FileExistsError):\n        p.symlink_to(p)\n"
    report = analyse(_repo(tmp_path, files={"code.py": code}))
    assert len(report.findings) == 1


def test_skipif_decorator_is_guarded(tmp_path):
    code = (
        "import os\nimport sys\nimport pytest\n"
        "@pytest.mark.skipif(sys.platform == 'win32', reason='x')\n"
        "def test_f(tmp_path):\n    os.symlink(tmp_path, tmp_path / 'l')\n"
    )
    report = analyse(_repo(tmp_path, files={"test_x.py": code}))
    assert not report.findings
    assert report.guarded == 1


def test_platform_conditional_is_guarded(tmp_path):
    code = "import os\nimport sys\ndef f(p):\n    if sys.platform != 'win32':\n        os.symlink(p, p)\n"
    report = analyse(_repo(tmp_path, files={"code.py": code}))
    assert not report.findings
    assert report.guarded == 1


def test_posix_only_call_flagged(tmp_path):
    report = analyse(_repo(tmp_path, files={"code.py": "import os\ndef f():\n    os.fork()\n"}))
    assert any("POSIX-only" in f.detail for f in report.findings)


def test_clean_repo_reports_nothing(tmp_path):
    repo = _repo(tmp_path, workflow="runs-on: ubuntu-latest\n", files={"ok.py": "x = 1\n"})
    report = analyse(repo)
    assert not report.findings
    assert "no platform or privilege gaps" in render(repo, report)


def test_verdict_mentions_windows_when_in_matrix(tmp_path):
    repo = _repo(
        tmp_path,
        workflow="runs-on: windows-latest\n",
        files={"code.py": "from pathlib import Path\ndef f(p):\n    p.symlink_to(p)\n"},
    )
    assert "Windows is in the CI matrix" in render(repo, analyse(repo))


def test_tracked_symlink_detected(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    target = tmp_path / "target.txt"
    target.write_text("hi", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks in this environment")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    assert "link.txt" in tracked_symlinks(tmp_path)


def test_links_in_tree_filters_symlinks():
    tree = {
        "tree": [
            {"path": "src/app.py", "mode": "100644"},
            {"path": "tests/pydantic_core", "mode": "120000"},
            {"path": "bin", "mode": "040000"},
            {"path": "CONTRIBUTING.md", "mode": "120000"},
        ],
        "truncated": False,
    }
    assert links_in_tree(tree) == (["CONTRIBUTING.md", "tests/pydantic_core"], False)


def test_links_in_tree_reports_truncation():
    # A truncated tree makes the count a lower bound, which the report has to say.
    assert links_in_tree({"tree": [], "truncated": True}) == ([], True)


def test_render_survey_separates_hits_from_misses():
    out = render_survey(
        [
            ("pydantic/pydantic", (["tests/pydantic_core"], False)),
            ("pallets/click", ([], False)),
            ("some/private", "HTTP 404"),
        ]
    )
    assert "pydantic/pydantic: 1 committed symlink(s)" in out
    assert "    tests/pydantic_core" in out
    assert "pallets/click: no committed symlinks" in out
    assert "some/private: HTTP 404" in out
    assert "1 of 3 worth cloning" in out
