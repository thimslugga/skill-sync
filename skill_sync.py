#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

"""skill-sync.py - Keep agent skill folders in sync with one central skills folder.

Every skill (a directory holding SKILL.md) in the source folder gets its own
symlink in every target folder, e.g. ~/.agents/skills, ~/.claude/skills and
~/.kiro/skills. Per-skill links are used instead of one link per folder
because some agents (Claude Code) have not reliably discovered skills through
a folder-level symlink, and none scan more than one level for a linked
"bundle" of skills.

Ownership rule: a symlink in a target folder is *managed* only when it points
into the source folder. Real files, real directories and symlinks that point
elsewhere are never modified or deleted; they are reported as conflicts.

Commands:
  sync      create, repair and prune links (default)
  status    show drift without changing anything; exit 1 when out of sync
  validate  check SKILL.md frontmatter against the Agent Skills spec
  watch     run `sync` every --interval seconds until interrupted

Exit codes: 0 success / in sync, 1 drift, invalid skills or failed actions,
2 configuration or usage error, 3 conflicts only (a real file, directory or
foreign symlink is in the way; needs manual cleanup).
"""

from __future__ import annotations

import argparse
import contextlib
import enum
import fcntl
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Sequence

log = logging.getLogger("skill-sync")

DEFAULT_TARGETS = ("~/.agents/skills", "~/.claude/skills", "~/.kiro/skills")

COMMANDS = ("sync", "status", "validate", "watch")

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*)$")

BLOCK_SCALARS = frozenset({">", "|", ">-", "|-", ">+", "|+"})
MAX_NAME_LEN = 64
MAX_DESCRIPTION_LEN = 1024
TMP_SUFFIX = ".skill-sync-tmp"
EXIT_CONFLICT = 3  # sync/status: only unmanaged paths in the way


class SkillSyncError(Exception):
    """Fatal configuration or runtime error; reported and exits with code 2."""


class Kind(enum.StrEnum):
    """What needs to happen to one link."""

    OK = "ok"
    CREATE = "create"
    REPLACE = "replace"
    PRUNE = "prune"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class Action:
    """One planned change (or non-change) for a path in a target folder."""

    kind: Kind
    link: Path
    dest: Path | None = None
    detail: str = ""


@dataclass(frozen=True)
class Config:
    """Effective settings after merging defaults, config file and CLI flags."""

    source: Path
    targets: tuple[Path, ...]
    pull: bool = False
    relative: bool = False
    strict: bool = False


# --------------------------------------------------------------------------
# Skill discovery and validation
# --------------------------------------------------------------------------


def discover_skills(source: Path) -> dict[str, Path]:
    """Return {name: path} for each direct child of `source` that holds SKILL.md."""
    if not source.is_dir():
        msg = f"source folder not found: {source}"
        raise SkillSyncError(msg)
    return {
        entry.name: entry
        for entry in sorted(source.iterdir())
        if not entry.name.startswith(".") and (entry / "SKILL.md").is_file()
    }


def read_frontmatter(skill_md: Path) -> dict[str, str]:
    """Parse the top-level scalar keys of a SKILL.md YAML frontmatter block.

    This is deliberately not a full YAML parser (the script stays stdlib-only).
    It handles `key: value`, quoted values, and indented continuation lines such
    as folded (`>`) or literal (`|`) block scalars, which covers how `name` and
    `description` are written in practice.
    """
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    current: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        match = KEY_RE.match(line)
        if match:
            current, value = match.group(1), match.group(2).strip()
            fields[current] = "" if value in BLOCK_SCALARS else value.strip("'\"")
        elif current and line[:1] in {" ", "\t"}:
            fields[current] = f"{fields[current]} {line.strip()}".strip()
    return {}  # no closing '---': treat as having no frontmatter


def validate_skill(name: str, path: Path) -> list[str]:
    """Return human-readable problems with a skill; empty when it is portable."""
    try:
        meta = read_frontmatter(path / "SKILL.md")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read SKILL.md: {exc}"]
    if not meta:
        return ["missing or unterminated YAML frontmatter (--- ... ---)"]
    problems: list[str] = []
    declared = meta.get("name", "")
    if not declared:
        problems.append("frontmatter has no 'name'")
    elif declared != name:
        problems.append(f"name '{declared}' does not match folder '{name}'")
    if declared and not NAME_RE.fullmatch(declared):
        problems.append("name must be lowercase letters, digits and single hyphens")
    if len(declared) > MAX_NAME_LEN:
        problems.append(f"name longer than {MAX_NAME_LEN} characters")
    description = meta.get("description", "")
    if not description:
        problems.append("frontmatter has no 'description'")
    elif len(description) > MAX_DESCRIPTION_LEN:
        problems.append(f"description longer than {MAX_DESCRIPTION_LEN} characters")
    return problems


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def link_dest(link: Path) -> Path:
    """Return where a symlink points, as an absolute but unresolved path."""
    raw = link.readlink()
    return raw if raw.is_absolute() else link.parent / raw


def is_managed(link: Path, source: Path) -> bool:
    """True when `link` is a symlink that points into the source folder."""
    if not link.is_symlink():
        return False
    dest = link_dest(link)
    lexical = Path(os.path.normpath(dest))
    if lexical.is_relative_to(Path(os.path.normpath(source))):
        return True
    # Also catch links written through a different path to the same place,
    # e.g. /var/home vs /home on Fedora Silverblue.
    return dest.resolve().is_relative_to(source.resolve())


def wanted_dest(link: Path, skill: Path, *, relative: bool) -> Path:
    """Return the value the symlink at `link` should hold."""
    if relative:
        return skill.relative_to(link.parent, walk_up=True)
    return skill


def plan_link(link: Path, want: Path, source: Path) -> Action:
    """Decide what to do with one skill's link in one target folder."""
    if link.is_symlink():
        current = link.readlink()
        if current == want:
            return Action(Kind.OK, link, want)
        if is_managed(link, source):
            return Action(Kind.REPLACE, link, want, f"was -> {current}")
        return Action(Kind.CONFLICT, link, want, f"foreign symlink -> {current}")
    if link.exists():
        return Action(Kind.CONFLICT, link, want, "real file or directory in the way")
    return Action(Kind.CREATE, link, want)


def plan_prune(target: Path, keep: Iterable[str], source: Path) -> list[Action]:
    """Plan removal of managed links whose skill is gone (or excluded)."""
    if not target.is_dir():
        return []
    wanted = set(keep)
    return [
        Action(Kind.PRUNE, entry, detail="no longer in source")
        for entry in sorted(target.iterdir())
        if entry.name not in wanted and is_managed(entry, source)
    ]


def plan_target(target: Path, skills: dict[str, Path], cfg: Config) -> list[Action]:
    """Plan every action for one target folder."""
    if target.is_symlink() and target.resolve() == cfg.source.resolve():
        log.warning(
            "%s is a symlink to the whole source folder; skipping it. Replace it "
            "with a real directory so per-skill links can be created.",
            target,
        )
        return []
    if target.is_symlink():
        log.warning("%s is itself a symlink; links will be created in its target", target)
    actions = [
        plan_link(
            target / name, wanted_dest(target / name, path, relative=cfg.relative), cfg.source
        )
        for name, path in skills.items()
    ]
    actions.extend(plan_prune(target, skills, cfg.source))
    return actions


def select_skills(cfg: Config) -> dict[str, Path]:
    """Discover skills, warn about invalid ones, and drop them in strict mode."""
    selected: dict[str, Path] = {}
    for name, path in discover_skills(cfg.source).items():
        problems = validate_skill(name, path)
        for problem in problems:
            log.warning("%s: %s", name, problem)
        if problems and cfg.strict:
            log.warning("%s: excluded (--strict)", name)
            continue
        selected[name] = path
    return selected


def build_plan(cfg: Config) -> list[Action]:
    """Plan actions across all target folders."""
    skills = select_skills(cfg)
    plan: list[Action] = []
    for target in cfg.targets:
        plan.extend(plan_target(target, skills, cfg))
    return plan


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------


def apply_action(action: Action) -> None:
    """Carry out one action. Links are swapped atomically via rename."""
    if action.kind is Kind.PRUNE:
        action.link.unlink()
        return
    if action.kind not in {Kind.CREATE, Kind.REPLACE} or action.dest is None:
        return
    action.link.parent.mkdir(parents=True, exist_ok=True)
    tmp = action.link.with_name(f".{action.link.name}{TMP_SUFFIX}")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(action.dest)
    tmp.replace(action.link)


def apply_plan(plan: Sequence[Action], *, dry_run: bool) -> int:
    """Apply a plan, report each change on stdout, return the failure count."""
    failures = 0
    for action in plan:
        if action.kind is Kind.OK:
            log.debug("ok       %s", tilde(action.link))
            continue
        print(describe(action))
        if action.kind is Kind.CONFLICT or dry_run:
            continue
        try:
            apply_action(action)
        except OSError as exc:
            failures += 1
            log.error("failed to %s %s: %s", action.kind, action.link, exc)  # noqa: TRY400  # expected I/O error, traceback adds nothing
    return failures


def run_git(git: str, source: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in `source`, capturing output; never raises on failure."""
    return subprocess.run(  # noqa: S603  # fixed argv, no shell; path comes from the user's own config
        [git, "-C", str(source), *args],
        check=False,
        timeout=120,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def git_pull(source: Path) -> None:
    """Fast-forward the git checkout that contains `source`, if there is one.

    A source that is not in a git repository, or whose branch has no upstream,
    is a normal setup (local-only skills), so the pull is skipped with an INFO
    message rather than a warning. A pull that is attempted and fails is
    logged as a warning, not raised: syncing what is already on disk is still
    right when the network or remote is down.
    """
    git = shutil.which("git")
    if git is None:
        log.warning("pull is enabled but git is not installed; skipping pull")
        return
    try:
        if run_git(git, source, "rev-parse", "--is-inside-work-tree").returncode != 0:
            log.info("pull skipped: %s is not in a git repository", tilde(source))
            return
        upstream = run_git(git, source, "rev-parse", "--abbrev-ref", "@{upstream}")
        if upstream.returncode != 0:
            log.info("pull skipped: branch in %s has no upstream", tilde(source))
            return
        result = run_git(git, source, "pull", "--ff-only", "--quiet")
    except subprocess.TimeoutExpired:
        log.warning("git pull timed out in %s", tilde(source))
        return
    if result.returncode != 0:
        # git splits one error over several lines; keep it to one log line.
        reason = " ".join(result.stderr.split()) or f"exit code {result.returncode}"
        log.warning("git pull failed in %s: %s", tilde(source), reason)


def runtime_dir() -> Path:
    """Return the directory for runtime files, per the XDG Base Directory spec.

    Uses $XDG_RUNTIME_DIR when it is set to an absolute path (the spec says to
    ignore relative values). Otherwise falls back, with a warning as the spec
    requires, to a per-user 0700 directory under the system temp dir, which
    like the real runtime dir is not meant to outlive a reboot.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_absolute():
        return Path(runtime)
    fallback = Path(tempfile.gettempdir()) / f"skill-sync-{os.getuid()}"
    log.warning(
        "$XDG_RUNTIME_DIR is %s; using %s for runtime files",
        "not an absolute path" if runtime else "not set",
        fallback,
    )
    fallback.mkdir(mode=0o700, exist_ok=True)
    info = fallback.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        msg = f"unsafe runtime directory (not a private directory you own): {fallback}"
        raise SkillSyncError(msg)
    return fallback


@contextlib.contextmanager
def sync_lock() -> Generator[None]:
    """Serialize sync passes (e.g. a timer firing while `watch` runs)."""
    lock_file = runtime_dir() / "skill-sync" / "lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


def tilde(path: Path) -> str:
    """Shorten paths under $HOME to ~/... for readable output."""
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def describe(action: Action) -> str:
    """One output line for an action."""
    text = f"{action.kind:<8} {tilde(action.link)}"
    if action.dest is not None and action.kind is not Kind.CONFLICT:
        text += f" -> {action.dest}"
    if action.detail:
        text += f"  ({action.detail})"
    return text


def summarize(plan: Sequence[Action]) -> str:
    """Count actions by kind, e.g. '2 create, 1 prune'."""
    counts = Counter(action.kind for action in plan)
    parts = [f"{counts[kind]} {kind}" for kind in Kind if counts[kind]]
    return ", ".join(parts) if parts else "nothing to do"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def has_conflicts(plan: Sequence[Action]) -> bool:
    """True when anything in the plan is blocked by an unmanaged path."""
    return any(action.kind is Kind.CONFLICT for action in plan)


def cmd_sync(cfg: Config, *, dry_run: bool) -> int:
    """Pull (optional), plan and apply."""
    with sync_lock():
        if cfg.pull and not dry_run:
            git_pull(cfg.source)
        plan = build_plan(cfg)
        failures = apply_plan(plan, dry_run=dry_run)
    prefix = "dry run: " if dry_run else ""
    log.info("%s%s", prefix, summarize(plan))
    if failures:
        return 1
    return EXIT_CONFLICT if has_conflicts(plan) else 0


def cmd_status(cfg: Config) -> int:
    """Report drift without changing anything."""
    plan = build_plan(cfg)
    drift = [action for action in plan if action.kind is not Kind.OK]
    for action in drift:
        print(describe(action))
    log.info("%s", summarize(plan))
    if any(action.kind is not Kind.CONFLICT for action in drift):
        return 1
    return EXIT_CONFLICT if drift else 0


def cmd_validate(cfg: Config) -> int:
    """Lint every skill's frontmatter."""
    bad = 0
    for name, path in discover_skills(cfg.source).items():
        problems = validate_skill(name, path)
        bad += bool(problems)
        for problem in problems:
            print(f"{name}: {problem}")
    log.info("%d skill(s) with problems", bad)
    return 1 if bad else 0


def cmd_watch(cfg: Config, *, interval: float) -> int:
    """Run sync on a fixed interval until interrupted."""
    log.info("watching %s every %ss (Ctrl-C to stop)", cfg.source, interval)
    try:
        while True:
            try:
                cmd_sync(cfg, dry_run=False)
            except SkillSyncError as exc:
                # Keep running: the source may be on a mount that comes back.
                log.error("%s", exc)  # noqa: TRY400  # message is complete; keep watching
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


# --------------------------------------------------------------------------
# Configuration and CLI
# --------------------------------------------------------------------------


def default_config_path() -> Path:
    """$XDG_CONFIG_HOME/skill-sync/config.toml (or ~/.config/...)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "skill-sync" / "config.toml"


def load_config_file(path: Path, *, required: bool) -> dict[str, Any]:
    """Read and type-check the TOML config file."""
    if not path.is_file():
        if required:
            msg = f"config file not found: {path}"
            raise SkillSyncError(msg)
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        msg = f"cannot read config {path}: {exc}"
        raise SkillSyncError(msg) from exc
    expected: dict[str, type] = {
        "source": str,
        "targets": list,
        "pull": bool,
        "relative": bool,
        "strict": bool,
    }
    for key, value in data.items():
        if key not in expected:
            msg = f"{path}: unknown key '{key}'"
            raise SkillSyncError(msg)
        if not isinstance(value, expected[key]):
            msg = f"{path}: '{key}' must be {expected[key].__name__}"
            raise SkillSyncError(msg)
    if not all(isinstance(t, str) for t in data.get("targets", [])):
        msg = f"{path}: 'targets' must be a list of strings"
        raise SkillSyncError(msg)
    return data


def expand(raw: str) -> Path:
    """Expand ~ and $VARS and make absolute (without resolving symlinks)."""
    return Path(os.path.abspath(Path(os.path.expandvars(raw)).expanduser()))  # noqa: PTH100  # Path.absolute() keeps '..'; abspath normalizes it without resolving links


def resolve_config(args: argparse.Namespace) -> Config:
    """Merge defaults < config file < command-line flags."""
    explicit = args.config is not None
    file_cfg = load_config_file(args.config or default_config_path(), required=explicit)
    source = args.source or file_cfg.get("source")
    if not source:
        msg = f"no source folder: pass --source or set 'source' in {default_config_path()}"
        raise SkillSyncError(msg)
    targets = args.target or file_cfg.get("targets") or list(DEFAULT_TARGETS)

    def pick(name: str) -> bool:
        cli = getattr(args, name)
        return bool(file_cfg.get(name, False)) if cli is None else bool(cli)

    return Config(
        source=expand(source),
        targets=tuple(dict.fromkeys(expand(t) for t in targets)),
        pull=pick("pull"),
        relative=pick("relative"),
        strict=pick("strict"),
    )


def build_parser() -> argparse.ArgumentParser:
    """Command-line interface."""
    parser = argparse.ArgumentParser(
        prog="skill-sync",
        description="Keep agent skill folders in sync with a central skills folder "
        "using one symlink per skill.",
        epilog="Config file: $XDG_CONFIG_HOME/skill-sync/config.toml "
        "(keys: source, targets, pull, relative, strict).",
    )
    parser.add_argument("command", nargs="?", choices=COMMANDS, default="sync")
    parser.add_argument("-c", "--config", type=Path, help="config file to use")
    parser.add_argument("-s", "--source", help="central folder holding <skill>/SKILL.md")
    parser.add_argument(
        "-t",
        "--target",
        action="append",
        help=f"agent skills folder; repeat for several (default: {', '.join(DEFAULT_TARGETS)})",
    )
    parser.add_argument(
        "--pull",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="git pull --ff-only in the source before syncing",
    )
    parser.add_argument(
        "--relative",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="write relative instead of absolute symlinks",
    )
    parser.add_argument(
        "--strict",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="do not link skills that fail validation (and prune their links)",
    )
    parser.add_argument("-n", "--dry-run", action="store_true", help="show changes only")
    parser.add_argument(
        "--interval", type=float, default=60.0, help="seconds between passes for 'watch'"
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="also show ok links")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="only warnings and errors")
    return parser


def configure_logging(level: int) -> None:
    """Send this tool's log records to stderr without touching the root logger."""
    log.setLevel(level)
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        log.addHandler(handler)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args = build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    configure_logging(level)
    try:
        cfg = resolve_config(args)
        if args.command == "status":
            return cmd_status(cfg)
        if args.command == "validate":
            return cmd_validate(cfg)
        if args.command == "watch":
            return cmd_watch(cfg, interval=args.interval)
        return cmd_sync(cfg, dry_run=args.dry_run)
    except SkillSyncError as exc:
        log.error("%s", exc)  # noqa: TRY400  # user-facing error, no traceback wanted
        return 2


if __name__ == "__main__":
    sys.exit(main())
