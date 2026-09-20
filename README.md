# ci-blindspot

Find what a project's CI **structurally cannot catch**.

A green CI badge means "the suite passed on the runners", not "the suite passes". Two
different gaps hide in that gap:

- **Platform gaps** — an OS nothing in the matrix runs on.
- **Privilege gaps** — an OS the matrix *does* run on, where the runner holds rights an
  ordinary developer does not.

The second one is the interesting case, because the badge looks like coverage. GitHub's
Windows runners can create symlinks. A normal Windows user cannot, without elevation or
Developer Mode. Anything gated on that privilege passes in CI forever and fails on
contributor machines forever — and no amount of matrix expansion fixes it, because the
matrix is already green.

```
python ci_blindspot.py path/to/repo
python ci_blindspot.py --self-check
```

No dependencies. Python 3.10+. Exits `1` when it finds something, `0` when it doesn't.

## Install

It's a single file with no dependencies — the simplest use is to run it directly:

```
python ci_blindspot.py path/to/repo
```

Or install the console command:

```
pip install git+https://github.com/iam-kira/ci-blindspot
ci-blindspot path/to/repo
```

## Does it work?

It was written after finding two of these by hand, then validated against three real
repositories — it reproduced all three independently. Sweeping further projects then found
three more, each verified by running the affected code on Windows.

| Project | Found | Verified how |
|---|---|---|
| pylint | 2 committed symlinks break a functional test | Full suite: only failure |
| MCP Python SDK | `symlink_to()` in a test, unguarded | Suite: 1 failed → 0 |
| lerobot | `update_last_checkpoint()` on the training path | Already upstream as #4059 |
| **mlflow** | `mlflow-skinny` builds an **empty wheel** | Built it: 6 entries, 0 `.py` files |
| **pipx** | Whole suite errors — `suppress(FileExistsError)` misses `OSError` | pytest: 5 errors → 5 passed |
| **black** | 3 symlink tests unguarded — regression of their own #287 | Suite: 3 failed → 0 failed |

The black one is the clearest illustration of the thesis. black fixed this exact failure
in 2018 (#287) by guarding one test. Tests added later did not carry the guard, and CI
never noticed, because CI's Windows runner can create symlinks. A bug can be fixed, come
back, and stay back — while the badge stays green the whole time.

Roughly half of a sweep's raw hits are not bugs. virtualenv and black's
`test_broken_symlink` were flagged and turned out correctly guarded; that is what
prompted the AST guard-detection pass, which drops optuna to zero findings and cuts
virtualenv from 10 to 7.

### pylint — the functional test that fails on every Windows checkout

```
CI platforms (12 workflow files): linux, macos, windows

Tracked symlinks (2) - break on a Windows checkout:
  tests/functional/s/symlink/_binding/__init__.py
  tests/functional/s/symlink/_binding/symlink_module.py

VERDICT: Windows is in the CI matrix, but the findings above are privilege-dependent.
```

Those two files are committed as symlinks (git mode `120000`). On Windows, git sets
`core.symlinks=false` for a non-elevated user and writes them out as **text files
containing the link target**. pylint lints that one-line path as Python, reports
`syntax-error`, and `test_functional[symlink_module0]` fails.

It is the only failure in a full run on Windows (`1 failed, 2182 passed`), and it has
been there long enough to be invisible — CI is green, because CI's runner materialises
the symlink.

Reported as [pylint-dev/pylint#11359](https://github.com/pylint-dev/pylint/issues/11359),
fixed in [#11360](https://github.com/pylint-dev/pylint/pull/11360).

### MCP Python SDK — a test that needs Administrator

```
CI platforms (10 workflow files): linux, windows
  never tested in CI: macos

Privilege-dependent calls (1):
  tests/shared/test_path_security.py:146
      creates a symlink (needs elevation or Developer Mode on Windows)
```

`test_safe_join_rejects_symlink_escape` calls `symlink_to()` unconditionally:

```
OSError: [WinError 1314] A required privilege is not held by the client
```

Same shape: a Windows job in CI, green, while the suite is red for every unprivileged
Windows contributor.

Reported as
[modelcontextprotocol/python-sdk#3408](https://github.com/modelcontextprotocol/python-sdk/issues/3408).

### lerobot — a product bug, not a test bug

```
CI platforms (14 workflow files): linux
  never tested in CI: windows, macos

Tracked symlinks (27) - break on a Windows checkout:
  ...

Privilege-dependent calls (1):
  src/lerobot/common/train_utils.py:129
      creates a symlink (needs elevation or Developer Mode on Windows)
```

That line is `update_last_checkpoint()`, reached from three call sites including the main
training entry point. On Windows it raises `WinError 1314` the first time a run saves a
checkpoint. Not a test failure — training itself.

Already reported upstream as
[huggingface/lerobot#4059](https://github.com/huggingface/lerobot/issues/4059); included
here because the tool found it from a cold start in under a second, and because 27
tracked symlinks in a repo with no Windows CI at all is its own result.

## What it checks

| Check | Why it matters |
|---|---|
| OS matrix across `.github/workflows/*.yml` | Which platforms are tested at all |
| Files committed as symlinks (git mode `120000`) | Materialise as text files on an unprivileged Windows checkout |
| `symlink_to` / `os.symlink` / `os.link` | Need elevation or Developer Mode on Windows |
| `os.fork` / `os.mkfifo` / `os.geteuid` | POSIX-only, no Windows equivalent |

## Limitations

Stated plainly, because a tool that overstates its confidence is worse than no tool.

- **It flags call sites, not bugs.** The AST pass understands `try/except OSError` and
  `with suppress(OSError)`, and reports those separately as a count. It does **not**
  understand `pytest.mark.skipif`, or a guard several frames up the call stack, so every
  finding still needs a human to confirm.
- **Guard detection is deliberately narrow.** Only exception types that actually cover a
  bare `OSError` count. `suppress(FileExistsError)` does not — that exact mistake is why
  pipx's entire test suite fails on Windows, so treating it as a guard would have hidden
  a real bug.
- **The workflow parse is deliberately crude.** It scans for runner names anywhere in the
  file rather than resolving YAML anchors and matrix expressions, so a platform named only
  in a disabled or conditional job still counts as covered. It errs toward reporting
  *more* coverage than exists, which makes a "not tested" verdict trustworthy and a
  "tested" verdict weaker.
- **Windows-centric.** The privilege asymmetry it understands is the Windows one. The same
  class of gap exists elsewhere (containers running as root, macOS entitlements) and is
  not covered.

## Why this class of bug persists

Test-suite failures on an under-tested platform are reported by nobody. A contributor
whose test run comes back red assumes they misconfigured their environment and moves on —
so the bug is rediscovered privately, repeatedly, and never filed. Product bugs get
reported because users hit them; test bugs do not, because the only people who see them
assume it is their own fault.

That asymmetry is the entire reason this tool finds anything.

## License

MIT — see [LICENSE](LICENSE).
