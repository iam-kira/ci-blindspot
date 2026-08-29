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

import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# runs-on values, plus bare matrix entries like `os: [ubuntu-latest, windows-latest]`
_RUNNER_RE = re.compile(r"(ubuntu|windows|macos)-[a-z0-9.]+", re.I)

_SYMLINK = "creates a symlink (needs elevation or Developer Mode on Windows)"
_POSIX_ONLY = "POSIX-only: no Windows equivalent"

# Calls that need a privilege a CI runner has and an ordinary user does not, keyed by
# the trailing dotted name of the call.
_PRIVILEGED_CALLS = {
    "symlink_to": _SYMLINK,
    "os.symlink": _SYMLINK,
    "os.link": "creates a hard link (restricted on some Windows filesystems)",
    "os.mkfifo": _POSIX_ONLY,
    "os.fork": _POSIX_ONLY,
    "os.geteuid": _POSIX_ONLY,
}

# Catching any of these covers WinError 1314, which arrives as a plain OSError.
# FileExistsError and friends are subclasses and do NOT cover it - that exact mistake
# is why pipx's whole test suite fails on Windows.
_BROAD_EXCEPTIONS = frozenset(
    {"OSError", "EnvironmentError", "IOError", "WindowsError", "Exception", "BaseException"}
)

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
    guarded: int = 0  # calls found but correctly handled; reported as a count only

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


def _dotted(node: ast.expr) -> str:
    """Best-effort dotted name for a call target: `a.b.c` -> "a.b.c"."""
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _match(dotted: str) -> str | None:
    """The reason this call is privilege-dependent, or None."""
    if not dotted:
        return None
    for key, why in _PRIVILEGED_CALLS.items():
        if dotted == key or dotted.endswith("." + key):
            return why
    # `p.symlink_to(...)` on any receiver
    if dotted.rsplit(".", 1)[-1] == "symlink_to":
        return _SYMLINK
    return None


def _names_in(node: ast.expr | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Tuple):
        return [n for e in node.elts for n in _names_in(e)]
    name = _dotted(node)
    return [name.rsplit(".", 1)[-1]] if name else []


def _handler_is_broad(handler: ast.ExceptHandler) -> bool:
    """True if this `except` would catch a bare OSError."""
    if handler.type is None:  # bare `except:`
        return True
    return any(n in _BROAD_EXCEPTIONS for n in _names_in(handler.type))


def _suppresses_broadly(item: ast.withitem) -> bool:
    """True for `with suppress(OSError)` and friends, false for suppress(FileExistsError)."""
    call = item.context_expr
    if not isinstance(call, ast.Call):
        return False
    if _dotted(call.func).rsplit(".", 1)[-1] != "suppress":
        return False
    return any(n in _BROAD_EXCEPTIONS for arg in call.args for n in _names_in(arg))


def _scan(node: ast.AST, guarded: bool, out: list[tuple[int, str, bool]]) -> None:
    """Walk the tree, tracking whether we are inside a guard that covers OSError."""
    if isinstance(node, ast.Try):
        inner = guarded or any(_handler_is_broad(h) for h in node.handlers)
        for child in node.body:
            _scan(child, inner, out)
        for handler in node.handlers:
            for child in handler.body:
                _scan(child, guarded, out)
        for child in [*node.orelse, *node.finalbody]:
            _scan(child, guarded, out)
        return

    if isinstance(node, (ast.With, ast.AsyncWith)):
        inner = guarded or any(_suppresses_broadly(i) for i in node.items)
        for child in node.body:
            _scan(child, inner, out)
        return

    if isinstance(node, ast.Call):
        why = _match(_dotted(node.func))
        if why:
            out.append((node.lineno, why, guarded))

    for child in ast.iter_child_nodes(node):
        _scan(child, guarded, out)


def privileged_calls(repo: Path) -> tuple[list[Finding], int]:
    """Unguarded privilege-dependent calls, plus a count of guarded ones."""
    findings: list[Finding] = []
    guarded_count = 0
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}

    for path in repo.rglob("*.py"):
        if skip & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue

        hits: list[tuple[int, str, bool]] = []
        _scan(tree, False, hits)
        rel = path.relative_to(repo).as_posix()
        for lineno, why, guarded in hits:
            if guarded:
                guarded_count += 1
            else:
                findings.append(Finding("privileged-call", f"{rel}:{lineno}", why))
    return findings, guarded_count


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
    calls, report.guarded = privileged_calls(repo)
    report.findings.extend(calls)
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
        out += ["", f"Unguarded privilege-dependent calls ({len(privileged)}):"]
        for f in privileged[:20]:
            out.append(f"  {f.location}\n      {f.detail}")
        if len(privileged) > 20:
            out.append(f"  ... and {len(privileged) - 20} more")

    if report.guarded:
        out.append(f"\n({report.guarded} further call(s) found, already guarded against OSError)")

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
        # One unguarded call, and four that are guarded in different ways. Only the
        # first should be reported. The suppress(FileExistsError) case is the pipx bug:
        # it looks like a guard and does not catch WinError 1314.
        (repo / "code.py").write_text(
            "import os\n"
            "from contextlib import suppress\n"
            "from pathlib import Path\n"
            "\n"
            "def unguarded(p):\n"
            "    (p / 'a').symlink_to(p)\n"
            "\n"
            "def guarded_try(p):\n"
            "    try:\n"
            "        (p / 'b').symlink_to(p)\n"
            "    except OSError:\n"
            "        pass\n"
            "\n"
            "def guarded_tuple(p):\n"
            "    try:\n"
            "        os.symlink(p, p / 'c')\n"
            "    except (OSError, NotImplementedError):\n"
            "        pass\n"
            "\n"
            "def guarded_suppress(p):\n"
            "    with suppress(OSError):\n"
            "        (p / 'd').symlink_to(p)\n"
            "\n"
            "def narrow_suppress(p):\n"
            "    with suppress(FileExistsError):\n"
            "        (p / 'e').symlink_to(p)\n",
            encoding="utf-8",
        )

        report = analyse(repo)

        assert report.ci_platforms == {"linux", "windows"}, report.ci_platforms
        assert report.uncovered == ["macos"], report.uncovered

        calls = sorted(f.location for f in report.findings if f.kind == "privileged-call")
        # line 6 = unguarded, line 26 = the narrow suppress that misses OSError
        assert calls == ["code.py:26", "code.py:6"], calls
        assert report.guarded == 3, report.guarded

        text = render(repo, report)
        assert "Windows is in the CI matrix" in text, text
        assert "already guarded" in text, text

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
