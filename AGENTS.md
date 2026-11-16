# AGENTS.md - Guidance for AI agents

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`skill_sync.py` is a single-file, stdlib-only Python 3.12+ CLI that keeps agent skill folders (`~/.agents/skills`, `~/.claude/skills`, `~/.kiro/skills`) in sync with one central source folder by maintaining **one symlink per skill** (a skill = a directory containing `SKILL.md`). The file is executable via a `uv run --script` shebang with `dependencies = []` — do not add third-party runtime dependencies.

## Commands

```bash
uv run pytest                              # all tests (pythonpath/testpaths set in pyproject.toml)
uv run pytest tests/test_skill_sync.py::test_sync_is_idempotent   # single test
uv run ruff check . && uv run ruff format --check .                    # lint (rule set in pyproject.toml)
uv run pyrefly check                                    # strict preset, untyped defs allowed
./skill_sync.py -n -c config.example.toml sync           # dry run against the example config
```

`pyproject.toml` is a ruff/pyrefly/pytest profile plus a `dev` dependency group (no build backend; `[project]` exists only to pin `requires-python`). Ruff `select` is broad; `D`, `ANN`, `T201` are ignored (print is the real output), max mccabe complexity is 12. Where a rule must be suppressed, the existing style is `# noqa: CODE  # reason`.

## Architecture

Pipeline in `skill_sync.py`: **discover → validate/select → plan → apply**, with `main()` dispatching `sync | status | validate | watch`.

- **Plan/apply separation.** `build_plan` produces immutable `Action(kind, link, dest, detail)` objects (`Kind`: OK/CREATE/REPLACE/PRUNE/CONFLICT) without touching disk. `sync` applies them; `status` and `--dry-run` just print them. New behaviors should be expressed as new planning logic, not side effects in apply.
- **Ownership rule (the core invariant).** A link in a target is *managed* only if it is a symlink pointing into the source folder (`is_managed`, which checks both lexical and resolved paths to handle `/var/home` vs `/home` aliasing). Real files/dirs and foreign symlinks are never modified or deleted — they surface as `CONFLICT`. Pruning only ever removes managed links (including dangling ones and leftover `.skill-sync-tmp` links).
- **Atomic link swaps.** `apply_action` creates a temp symlink (`TMP_SUFFIX`) then `replace()`s it over the real name.
- **Whole-folder symlink targets** (target itself symlinks to source) are skipped with a warning in `plan_target`, since per-skill links can't be created there.
- **Frontmatter parsing is intentionally not YAML.** `read_frontmatter` is a minimal scalar/block-scalar reader to stay stdlib-only; `validate_skill` checks name (matches folder, `NAME_RE`, ≤64) and description (≤1024). `strict` excludes invalid skills from the plan, which prunes their links.
- **Config precedence:** defaults < `$XDG_CONFIG_HOME/skill-sync/config.toml` < CLI flags (`resolve_config`). The config file is type-checked and unknown keys are an error. Boolean options use `BooleanOptionalAction` with `default=None` so "unset" is distinguishable from `False`.
- **Concurrency/failure:** `sync_lock` is an `flock` on `<runtime_dir()>/skill-sync/lock` (timer vs. `watch`). `runtime_dir()` follows the XDG Base Directory spec: absolute `$XDG_RUNTIME_DIR`, else (relative or unset, with a warning) a verified-private `$TMPDIR/skill-sync-<uid>` dir. The temp symlinks used for atomic swaps deliberately live in the target folder, not the runtime dir, since `rename` must stay on one filesystem. `git_pull` (via `run_git`, `check=False`, `GIT_TERMINAL_PROMPT=0` so unattended runs never hang on credentials) first checks the source is in a work tree with an upstream; if not, it skips at INFO level since local-only skills are a normal setup. A pull that is attempted and fails or times out only warns; sync continues with what's on disk.
- **Exit codes:** 0 ok/in sync, 1 drift/invalid skills/failed actions, 2 config or usage error (`SkillSyncError`), 3 (`EXIT_CONFLICT`) only conflicts remain (unmanaged path in the way; `sync` and `status`, 1 takes precedence).

## Tests

`tests/test_skill_sync.py` imports the module as `skill_sync as ss` and drives `ss.main([...])` end-to-end against `tmp_path` via the `env` fixture (monkeypatched HOME/XDG). Prefer adding behavior tests through `main()` over calling internals. Git is faked by patching `subprocess.run` with the `fake_git` helper, which returns `CompletedProcess` results (the code no longer relies on `CalledProcessError`).

## contrib/

Deployment units, not code: systemd `skill-sync.{service,timer,path}` and a macOS launchd plist. They expect the script installed as `~/.local/bin/skill-sync`, and their watched source path (`PathChanged` / `WatchPaths`) is hard-coded and must be edited to match the user's `source`.
