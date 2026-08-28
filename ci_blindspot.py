#!/usr/bin/env python3
"""Find what a project's CI structurally cannot catch.

A green CI badge means "the suite passed on the runners", not "the suite passes".
Two gaps hide in that difference:

  * platform gaps  - an OS nothing in the matrix runs on
  * privilege gaps - an OS the matrix *does* run on, where the runner has rights
                     an ordinary developer does not

The second is the interesting one, because the badge looks like coverage. GitHub's
Windows runners can create symlinks; a normal Windows user cannot without elevation
or Developer Mode. Anything gated on that privilege passes in CI forever and fails
on contributor machines forever.

Usage:
    python ci_blindspot.py [path-to-repo]
    python ci_blindspot.py --self-check
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# runs-on values, plus bare matrix entries like `os: [ubuntu-latest, windows-latest]`
_RUNNER_RE = re.compile(r"(ubuntu|windows|macos)-[a-z0-9.]+", re.I)

# Calls that need elevation or Developer Mode on Windows. A CI runner has both, so
# these are invisible to a green windows-latest job.
_PRIVILEGED_CALLS = {
    ".symlink_to(": "creates a symlink (needs elevation or Developer Mode on Windows)",
    "os.symlink(": "creates a symlink (needs elevation or Developer Mode on Windows)",
    "os.link(": "creates a hard link (restricted on some Windows filesystems)",
    "os.mkfifo(": "POSIX-only: no Windows equivalent",
    "os.fork(": "POSIX-only: no Windows equivalent",
    "os.geteuid(": "POSIX-only: no Windows equivalent",
}

PLATFORMS = ("linux", "windows", "macos")


@dataclass
class Finding:
    kind: str
    location: str
    detail: str


@dataclass
class Report:
    ci_platforms: set[str] = field(default_factory=set)
    workflows: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def uncovered(self) -> list[str]:
        return [p for p in PLATFORMS if p not in self.ci_platforms]


def ci_platforms(repo: Path) -> tuple[set[str], int]:
    """Platforms named anywhere in the workflow files, and how many files there are."""
    workflow_dir = repo / ".github" / "workflows"
    if not workflow_dir.is_dir():
        return set(), 0

    found: set[str] = set()
    files = sorted(workflow_dir.glob("*.y*ml"))
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _RUNNER_RE.finditer(text):
            os_name = match.group(1).lower()
            found.add("linux" if os_name == "ubuntu" else os_name)
    return found, len(files)


def tracked_symlinks(repo: Path) -> list[str]:
    """Symlinks committed to git (mode 120000).

    On Windows, git sets core.symlinks=false for a non-elevated user and writes these
    out as ordinary text files containing the link target. Anything that reads them
    expecting the linked content gets a one-line path instead.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-s"],
            capture_output=True, text=True, timeout=60, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [
        line.split("\t", 1)[1]
        for line in out.splitlines()
        if line.startswith("120000") and "\t" in line
    ]


def privileged_calls(repo: Path) -> list[Finding]:
    """Source-level calls that a privileged CI runner will never fail on."""
    findings: list[Finding] = []
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}

    for path in repo.rglob("*.py"):
        if skip & set(path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue
            for needle, why in _PRIVILEGED_CALLS.items():
                if needle in line:
                    rel = path.relative_to(repo).as_posix()
                    findings.append(Finding("privileged-call", f"{rel}:{lineno}", why))
    return findings


def analyse(repo: Path) -> Report:
    report = Report()
    report.ci_platforms, report.workflows = ci_platforms(repo)

    for link in tracked_symlinks(repo):
        report.findings.append(
            Finding(
                "tracked-symlink",
                link,
                "committed as a symlink; a non-elevated Windows checkout gets a text "
                "file holding the link target instead",
            )
        )
    report.findings.extend(privileged_calls(repo))
    return report


def render(repo: Path, report: Report) -> str:
    out: list[str] = [f"ci-blindspot: {repo}", ""]

    if report.workflows:
        covered = ", ".join(sorted(report.ci_platforms)) or "none detected"
        out.append(f"CI platforms ({report.workflows} workflow files): {covered}")
    else:
        out.append("CI platforms: no .github/workflows found")

    if report.uncovered:
        out.append(f"  never tested in CI: {', '.join(report.uncovered)}")

    windows_in_ci = "windows" in report.ci_platforms
    privileged = [f for f in report.findings if f.kind != "tracked-symlink"]
    symlinks = [f for f in report.findings if f.kind == "tracked-symlink"]

    if symlinks:
        out += ["", f"Tracked symlinks ({len(symlinks)}) - break on a Windows checkout:"]
        out += [f"  {f.location}" for f in symlinks[:20]]
        if len(symlinks) > 20:
            out.append(f"  ... and {len(symlinks) - 20} more")

    if privileged:
        out += ["", f"Privilege-dependent calls ({len(privileged)}):"]
        for f in privileged[:20]:
            out.append(f"  {f.location}\n      {f.detail}")
        if len(privileged) > 20:
            out.append(f"  ... and {len(privileged) - 20} more")

    out.append("")
    if report.findings and windows_in_ci:
        out.append(
            "VERDICT: Windows is in the CI matrix, but the findings above are "
            "privilege-dependent.\n"
            "         GitHub's Windows runners hold rights an ordinary user does not, so "
            "these\n"
            "         pass in CI and fail on a contributor's machine. The badge is green "
            "either way."
        )
    elif report.findings:
        out.append(
            "VERDICT: findings above are platform-sensitive and Windows is not in the CI "
            "matrix."
        )
    else:
        out.append("VERDICT: no platform or privilege gaps detected.")
    return "\n".join(out)


def self_check() -> int:
    """Build a repo shaped like the real bugs and prove each detector fires."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "test.yml").write_text(
            "jobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "  b:\n    runs-on: windows-latest\n",
            encoding="utf-8",
        )
        (repo / "code.py").write_text(
            "from pathlib import Path\n"
            "def f(p):\n"
            "    (p / 'link').symlink_to(p)\n"
            "# os.symlink( in a comment must not count\n",
            encoding="utf-8",
        )

        report = analyse(repo)

        assert report.ci_platforms == {"linux", "windows"}, report.ci_platforms
        assert report.uncovered == ["macos"], report.uncovered

        calls = [f for f in report.findings if f.kind == "privileged-call"]
        assert len(calls) == 1, f"expected 1 privileged call, got {calls}"
        assert calls[0].location == "code.py:3", calls[0].location

        text = render(repo, report)
        assert "Windows is in the CI matrix" in text, text

        # A clean repo must produce no findings and say so.
        clean = Path(tmp) / "clean"
        (clean / ".github" / "workflows").mkdir(parents=True)
        (clean / ".github" / "workflows" / "t.yml").write_text(
            "runs-on: ubuntu-latest\n", encoding="utf-8"
        )
        (clean / "ok.py").write_text("x = 1\n", encoding="utf-8")
        clean_report = analyse(clean)
        assert not clean_report.findings, clean_report.findings
        assert "no platform or privilege gaps" in render(clean, clean_report)

    print("self-check passed")
    return 0


def main(argv: list[str]) -> int:
    if "--self-check" in argv:
        return self_check()

    repo = Path(argv[1] if len(argv) > 1 else ".").resolve()
    if not repo.is_dir():
        print(f"not a directory: {repo}", file=sys.stderr)
        return 2

    report = analyse(repo)
    print(render(repo, report))
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
