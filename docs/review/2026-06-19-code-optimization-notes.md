# 2026-06-19 Code Optimization Notes

| Item | Value |
| --- | --- |
| Scope | safe code cleanup, test collection defaults, repository hygiene |
| Constraint | do not change core runtime logic |
| Date | 2026-06-19 |

## Completed Low-Risk Optimizations

### Typed git command result

`app/task_runtime/session_workspace.py` had repeated `subprocess.run(...)` handling for git calls. The shared helper now returns a typed `GitCommandResult` dataclass instead of a loose dictionary, and git error formatting is centralized.

This keeps the same behavior for:

- validating git worktrees;
- creating node worktrees or clone fallbacks;
- auto-committing node repository changes;
- reading git stdout.

### Default pytest collection boundary

The repository root contains runtime data, generated previews, caches, and directories that can be unreadable in local Windows runs. `pyproject.toml` now limits default pytest discovery to `tests/` and excludes known generated/runtime directories.

This does not affect production code, but makes `pytest` from the project root reproducible.

## Design Issues To Address Later

### Runtime data and generated artifacts are too easy to create at repository root

Current evidence:

- root-level probe databases exist locally: `pythonProjectjarvisdataknowledge.db`, `sqlite_probe_root.db`, `sqlite_probe_root.db-journal`;
- root-level build outputs exist locally: `build/`, `dist/`;
- cache and coverage outputs exist locally: `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `htmlcov/`.

Problem:

The codebase already ignores many of these paths, but tooling still allows them to appear in the repository root. That makes local state noisy and increases the chance that future tests or scripts depend on accidental files.

Recommendation:

- Make scripts and probes write under `data/`, `sandbox/`, or a caller-provided temp directory.
- Add explicit output directory arguments to scripts that currently default to the repository root.
- Keep root only for source, checked-in docs, lockfiles, and stable project metadata.

### Packaging support is present but not documented as a workflow

Current evidence:

- `pyinstaller` is present in the dev dependency group;
- `.gitignore` ignores `*.spec`, `build/`, and `dist/`.

Problem:

The project has the pieces of a PyInstaller workflow, but no checked-in command, script, or document explains the supported entry point, output location, or artifact retention policy. This encourages ad hoc local builds.

Recommendation:

- Add a small packaging script or documentation page if PyInstaller is officially supported.
- Otherwise remove the dependency and keep packaging out of the default dev environment.

### Architecture diagrams are duplicated across root and docs

Current evidence:

- root contains several architecture image variants: `jarvis-architecture*.png`, `jarvis_architecture.svg`;
- `docs/` also contains architecture images: `docs/architecture.svg`, `docs/jarvis_architecture.png`, `docs/jarvis_architecture_v4.png`.
- tests and design docs still reference `jarvis-architecture-v3.png` and `docs/jarvis_architecture.png`.

Problem:

Multiple similar diagram files make it unclear which one is canonical. This is a documentation ownership issue rather than runtime logic.

Recommendation:

- Pick one canonical location under `docs/`.
- Archive or remove obsolete variants after confirming they are not referenced by README or design docs.
- Name diagram files with version/date only when multiple versions are intentionally preserved.

### Node workspace write-back remains a larger product design gap

The previous review in `docs/review/2026-06-13-workspace-refactor-code-review.md` identified that isolated node repositories can produce commits that are not integrated back to the canonical repository.

This note does not change that behavior. It remains a main-logic design decision and should be handled in a dedicated change.

## Cleanup Performed

Removed local generated files where permissions allowed:

- `build/`
- `dist/`
- `htmlcov/`
- `.mypy_cache/`
- `.ruff_cache/`
- `.pytest_tmp/`
- `.pytest_tmp_cases/`
- root-level SQLite probe files: `pythonProjectjarvisdataknowledge.db`, `sqlite_probe_root.db`, `sqlite_probe_root.db-journal`
- discovered `__pycache__/` directories

Not removed:

- `.pytest_cache/` and `pytest_tmp_codex_tool_log_fix3/`, because Windows denied access;
- ignored runtime/business data under `data/`, `logs/`, `sandbox/`, and `sessions/`, because those may contain useful local state.

## Verification

Commands run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_session_workspace.py -q
.venv\Scripts\python.exe -m pytest --collect-only -q
.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Results:

- `tests\test_session_workspace.py`: 5 passed.
- default collection: 328 tests collected from `tests/`.
- full default suite: 322 passed, 6 skipped, 1 warning.
- `git diff --check`: no whitespace errors; Git only reported local LF-to-CRLF warnings.
