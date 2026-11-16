"""Tests for skill_sync."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

import skill_sync as ss


def make_skill(
    source: Path,
    name: str,
    *,
    declared: str | None = None,
    desc: str = "Does X.",
    extra: str = "",
) -> Path:
    skill = source / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {declared or name}\ndescription: {desc}\n{extra}---\n# {name}\n",
        encoding="utf-8",
    )
    return skill


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    source = tmp_path / "central" / "skills"
    make_skill(source, "pr-review")
    make_skill(source, "aws-debug")
    return {
        "root": tmp_path,
        "source": source,
        "claude": tmp_path / "home" / ".claude" / "skills",
        "agents": tmp_path / "home" / ".agents" / "skills",
    }


def run(env: dict[str, Path], *extra: str) -> int:
    args = ["-s", str(env["source"]), "-t", str(env["claude"]), "-t", str(env["agents"])]
    return ss.main([*args, *extra])


# --- discovery and validation ------------------------------------------------


def test_discover_ignores_hidden_and_non_skill_dirs(env: dict[str, Path]) -> None:
    (env["source"] / "not-a-skill").mkdir()
    (env["source"] / ".git").mkdir()
    (env["source"] / ".git" / "SKILL.md").write_text("x", encoding="utf-8")
    assert list(ss.discover_skills(env["source"])) == ["aws-debug", "pr-review"]


def test_discover_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(ss.SkillSyncError, match="source folder not found"):
        ss.discover_skills(tmp_path / "nope")


def test_frontmatter_folded_description_and_quotes(tmp_path: Path) -> None:
    md = tmp_path / "SKILL.md"
    md.write_text(
        "---\nname: 'demo'\ndescription: >\n  Line one\n  line two.\n"
        "metadata:\n  a: b\n---\nbody\n",
        encoding="utf-8",
    )
    meta = ss.read_frontmatter(md)
    assert meta["name"] == "demo"
    assert meta["description"] == "Line one line two."


@pytest.mark.parametrize("text", ["no frontmatter\n", "---\nname: x\n", ""])
def test_frontmatter_missing_or_unterminated(tmp_path: Path, text: str) -> None:
    md = tmp_path / "SKILL.md"
    md.write_text(text, encoding="utf-8")
    assert ss.read_frontmatter(md) == {}


def test_validate_reports_problems(tmp_path: Path) -> None:
    good = make_skill(tmp_path, "good")
    mismatch = make_skill(tmp_path, "folder", declared="other")
    bad_chars = make_skill(tmp_path, "Bad_Name")
    no_desc = make_skill(tmp_path, "no-desc", desc="")
    long_desc = make_skill(tmp_path, "long", desc="x" * 1100)
    no_front = tmp_path / "plain"
    no_front.mkdir()
    (no_front / "SKILL.md").write_text("# hi\n", encoding="utf-8")

    assert ss.validate_skill("good", good) == []
    assert "does not match folder" in ss.validate_skill("folder", mismatch)[0]
    assert any("lowercase" in p for p in ss.validate_skill("Bad_Name", bad_chars))
    assert ss.validate_skill("no-desc", no_desc) == ["frontmatter has no 'description'"]
    assert "longer than" in ss.validate_skill("long", long_desc)[0]
    assert "frontmatter" in ss.validate_skill("plain", no_front)[0]


def test_validate_accepts_every_spec_field(tmp_path: Path) -> None:
    extra = (
        "license: MIT\ncompatibility: Needs git\nallowed-tools: Bash(git:*)\n"
        "metadata:\n  author: me\n"
    )
    skill = make_skill(tmp_path, "full", extra=extra)
    assert ss.validate_skill("full", skill) == []
    assert ss.unknown_fields(skill) == []


def test_validate_rejects_long_compatibility(tmp_path: Path) -> None:
    skill = make_skill(tmp_path, "compat", extra=f"compatibility: {'x' * 501}\n")
    assert any("compatibility" in p for p in ss.validate_skill("compat", skill))
    ok = make_skill(tmp_path, "compat-ok", extra=f"compatibility: {'x' * 500}\n")
    assert ss.validate_skill("compat-ok", ok) == []


def test_unknown_fields_lists_keys_outside_the_spec(tmp_path: Path) -> None:
    skill = make_skill(tmp_path, "extra", extra="version: 2\nmodel: opus\n")
    assert ss.unknown_fields(skill) == ["version", "model"]


def test_validate_command_warns_on_unknown_field_without_failing(
    env: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    make_skill(env["source"], "extra", extra="version: 2\n")
    assert run(env, "validate") == 0
    assert "extra: unknown frontmatter field 'version'" in capsys.readouterr().out


def test_unknown_field_warns_on_sync_and_is_linked_even_when_strict(
    env: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    make_skill(env["source"], "extra", extra="version: 2\n")
    assert run(env, "--strict") == 0
    assert (env["claude"] / "extra").is_symlink()
    assert "unknown frontmatter field 'version'" in caplog.text


def test_strict_excludes_long_compatibility(env: dict[str, Path]) -> None:
    make_skill(env["source"], "wordy", extra=f"compatibility: {'x' * 501}\n")
    run(env, "--strict")
    assert not (env["claude"] / "wordy").exists()


# --- sync behaviour -------------------------------------------------------------


def test_sync_creates_per_skill_links_in_every_target(env: dict[str, Path]) -> None:
    assert run(env) == 0
    for target in (env["claude"], env["agents"]):
        assert target.is_dir()
        assert not target.is_symlink()
        for name in ("pr-review", "aws-debug"):
            link = target / name
            assert link.is_symlink()
            assert link.readlink() == env["source"] / name
            assert (link / "SKILL.md").is_file()


def test_sync_is_idempotent(env: dict[str, Path]) -> None:
    run(env)
    cfg = ss.Config(env["source"], (env["claude"], env["agents"]))
    assert {a.kind for a in ss.build_plan(cfg)} == {ss.Kind.OK}


def test_prunes_links_for_removed_skills_including_dangling(env: dict[str, Path]) -> None:
    make_skill(env["source"], "old-one")
    run(env)
    (env["source"] / "old-one" / "SKILL.md").unlink()
    (env["source"] / "old-one").rmdir()
    assert run(env) == 0
    assert not (env["claude"] / "old-one").is_symlink()
    assert not (env["agents"] / "old-one").is_symlink()


def test_never_touches_real_dirs_or_foreign_links(env: dict[str, Path]) -> None:
    env["claude"].mkdir(parents=True)
    real = env["claude"] / "pr-review"
    real.mkdir()
    elsewhere = env["root"] / "elsewhere"
    elsewhere.mkdir()
    foreign = env["claude"] / "aws-debug"
    foreign.symlink_to(elsewhere)
    unrelated = env["claude"] / "mine"
    unrelated.symlink_to(elsewhere)

    assert run(env) == ss.EXIT_CONFLICT
    assert real.is_dir()
    assert not real.is_symlink()
    assert foreign.readlink() == elsewhere
    assert unrelated.readlink() == elsewhere
    cfg = ss.Config(env["source"], (env["claude"],))
    kinds = {a.link.name: a.kind for a in ss.build_plan(cfg)}
    assert kinds == {"pr-review": ss.Kind.CONFLICT, "aws-debug": ss.Kind.CONFLICT}


def test_replaces_managed_link_pointing_at_wrong_skill(env: dict[str, Path]) -> None:
    env["claude"].mkdir(parents=True)
    wrong = env["claude"] / "pr-review"
    wrong.symlink_to(env["source"] / "aws-debug")
    run(env)
    assert wrong.readlink() == env["source"] / "pr-review"


def test_relative_links_resolve_to_skill(env: dict[str, Path]) -> None:
    assert run(env, "--relative") == 0
    link = env["claude"] / "pr-review"
    assert not link.readlink().is_absolute()
    assert link.resolve() == (env["source"] / "pr-review").resolve()
    # Switching back to absolute rewrites the managed links.
    run(env, "--no-relative")
    assert link.readlink().is_absolute()


@pytest.mark.parametrize("alias", ["target", "parent"])
def test_relative_links_resolve_through_target_alias(env: dict[str, Path], alias: str) -> None:
    actual = env["root"] / "elsewhere" / "deep" / "nested" / "actual"
    actual.mkdir(parents=True)
    path = env["claude"] if alias == "target" else env["claude"].parent
    path.parent.mkdir(parents=True)
    path.symlink_to(actual)

    assert run(env, "--relative") == 0
    for name in ("pr-review", "aws-debug"):
        link = env["claude"] / name
        assert link.exists()
        assert not link.readlink().is_absolute()
        assert link.resolve() == (env["source"] / name).resolve()
    assert run(env, "--relative", "status") == 0
    assert run(env, "--relative") == 0


def test_managed_detected_through_symlinked_source_path(env: dict[str, Path]) -> None:
    alias = env["root"] / "alias"
    alias.symlink_to(env["source"].parent)
    env["claude"].mkdir(parents=True)
    old = env["claude"] / "gone"
    old.symlink_to(alias / "skills" / "pr-review")
    run(env)
    assert not old.is_symlink()


def test_whole_dir_symlink_target_is_skipped(
    env: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    env["claude"].parent.mkdir(parents=True)
    env["claude"].symlink_to(env["source"])
    assert run(env) == 0
    assert env["claude"].is_symlink()
    assert "whole source folder" in caplog.text


def test_strict_excludes_invalid_and_prunes_their_links(env: dict[str, Path]) -> None:
    make_skill(env["source"], "broken", declared="not-broken")
    run(env)
    assert (env["claude"] / "broken").is_symlink()
    run(env, "--strict")
    assert not (env["claude"] / "broken").exists()
    assert (env["claude"] / "pr-review").is_symlink()


def test_dry_run_changes_nothing(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(env, "--dry-run") == 0
    assert not env["claude"].exists()
    assert "create" in capsys.readouterr().out


@pytest.mark.parametrize("replace", [False, True])
def test_leftover_tmp_link_is_pruned(env: dict[str, Path], replace: bool) -> None:
    env["claude"].mkdir(parents=True)
    link = env["claude"] / "pr-review"
    if replace:
        link.symlink_to(env["source"] / "aws-debug")
    tmp = env["claude"] / f".pr-review{ss.TMP_SUFFIX}"
    tmp.symlink_to(env["source"] / "pr-review")
    assert run(env) == 0
    assert not tmp.is_symlink()
    assert link.resolve() == (env["source"] / "pr-review").resolve()
    assert run(env, "status") == 0


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling-symlink"])
@pytest.mark.parametrize("replace", [False, True])
def test_unmanaged_tmp_path_is_a_conflict(env: dict[str, Path], kind: str, replace: bool) -> None:
    env["claude"].mkdir(parents=True)
    link = env["claude"] / "pr-review"
    if replace:
        link.symlink_to(env["source"] / "aws-debug")
    tmp = env["claude"] / f".pr-review{ss.TMP_SUFFIX}"
    foreign = env["root"] / "foreign"
    if kind == "file":
        tmp.write_text("user data", encoding="utf-8")
    elif kind == "directory":
        tmp.mkdir()
    else:
        if kind == "symlink":
            foreign.mkdir()
        tmp.symlink_to(foreign)
    original_inode = tmp.lstat().st_ino

    assert run(env) == ss.EXIT_CONFLICT
    assert tmp.lstat().st_ino == original_inode
    if replace:
        assert link.readlink() == env["source"] / "aws-debug"
    else:
        assert not link.exists()
    assert (env["claude"] / "aws-debug").exists()
    assert run(env, "status") == ss.EXIT_CONFLICT
    if kind == "file":
        assert tmp.read_text(encoding="utf-8") == "user data"
    elif kind in {"symlink", "dangling-symlink"}:
        assert tmp.readlink() == foreign


def test_tmp_file_created_after_planning_is_preserved(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_action = ss.apply_action
    tmp = env["claude"] / f".pr-review{ss.TMP_SUFFIX}"

    def create_file_then_apply(action: ss.Action) -> None:
        if action.link == env["claude"] / "pr-review":
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text("user data", encoding="utf-8")
        apply_action(action)

    monkeypatch.setattr(ss, "apply_action", create_file_then_apply)
    assert run(env) == 1
    assert tmp.read_text(encoding="utf-8") == "user data"
    assert not (env["claude"] / "pr-review").exists()


def test_failed_swap_recovers_on_next_sync(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replace(_path: Path, target: Path) -> Path:
        raise PermissionError(target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        assert run(env) == 1
    assert run(env) == 0
    assert run(env, "status") == 0
    assert not list(env["claude"].glob(f"*{ss.TMP_SUFFIX}"))


def test_apply_failure_returns_1(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(action: ss.Action) -> None:
        raise PermissionError(action.link)

    monkeypatch.setattr(ss, "apply_action", boom)
    assert run(env) == 1


# --- status / validate / config -------------------------------------------------


def test_default_target_is_one_folder(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(env["root"] / "home"))
    args = ss.build_parser().parse_args(["-s", str(env["source"])])
    assert ss.resolve_config(args).targets == (env["agents"],)
    assert ss.main(["-s", str(env["source"])]) == 0
    assert (env["agents"] / "pr-review").exists()
    assert not env["claude"].exists()


def test_status_exit_codes(env: dict[str, Path]) -> None:
    assert run(env, "status") == 1
    run(env)
    assert run(env, "status") == 0


def test_validate_command(env: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert run(env, "validate") == 0
    make_skill(env["source"], "x", declared="y")
    assert run(env, "validate") == 1
    assert "does not match" in capsys.readouterr().out


def test_config_file_and_cli_override(env: dict[str, Path]) -> None:
    cfg_file = env["root"] / "cfg" / "skill-sync" / "config.toml"
    cfg_file.parent.mkdir(parents=True)
    cfg_file.write_text(
        f'source = "{env["source"]}"\ntargets = ["{env["claude"]}"]\nrelative = true\n',
        encoding="utf-8",
    )
    assert ss.main([]) == 0
    assert not (env["claude"] / "pr-review").readlink().is_absolute()
    assert not env["agents"].exists()
    assert ss.main(["--no-relative"]) == 0
    assert (env["claude"] / "pr-review").readlink().is_absolute()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("bogus = 1\n", "unknown key"),
        ("source = 5\n", "must be str"),
        ('targets = [1]\nsource = "/x"\n', "list of strings"),
        ("source = [\n", "cannot read config"),
    ],
)
def test_bad_config_exits_2(
    tmp_path: Path, content: str, message: str, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(content, encoding="utf-8")
    assert ss.main(["-c", str(cfg_file)]) == 2
    assert message in caplog.text


def test_missing_source_and_missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert ss.main([]) == 2
    assert ss.main(["-c", str(tmp_path / "missing.toml")]) == 2
    assert ss.main(["-s", str(tmp_path / "nope"), "-t", str(tmp_path / "t")]) == 2


def fake_git(
    monkeypatch: pytest.MonkeyPatch, results: dict[str, tuple[int, str]]
) -> list[list[str]]:
    """Patch subprocess.run; `results` maps `pull` or a rev-parse arg to (returncode, stderr)."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"  # type: ignore[index]
        key = argv[4] if argv[3] == "rev-parse" else argv[3]
        code, err = results.get(key, (0, ""))
        return subprocess.CompletedProcess(argv, code, "", err)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/git")
    return calls


def test_pull_runs_git_and_survives_failure(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = fake_git(monkeypatch, {"pull": (1, "fatal: no\nremote\n")})
    assert run(env, "--pull") == 0
    assert calls[-1][1:] == ["-C", str(env["source"]), "pull", "--ff-only", "--quiet"]
    assert "git pull failed" in caplog.text
    assert "fatal: no remote" in caplog.text  # multi-line stderr folded to one line
    assert (env["claude"] / "pr-review").is_symlink()


@pytest.mark.parametrize(
    ("failing", "message"),
    [("--is-inside-work-tree", "not in a git repository"), ("--abbrev-ref", "has no upstream")],
)
def test_pull_skipped_quietly_without_repo_or_upstream(
    env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing: str,
    message: str,
) -> None:
    calls = fake_git(monkeypatch, {failing: (128, "fatal")})
    assert run(env, "--pull") == 0
    assert message in caplog.text
    assert "WARNING" not in caplog.text
    assert not any("pull" in c for c in calls)


def test_pull_timeout_is_a_warning(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def hang(argv: list[str], **_: object) -> None:
        raise subprocess.TimeoutExpired(argv, 120)

    monkeypatch.setattr(subprocess, "run", hang)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/git")
    assert run(env, "--pull") == 0
    assert "timed out" in caplog.text


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_runtime_dir_uses_absolute_xdg_runtime_dir(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(ss.sys, "platform", platform)
    assert ss.runtime_dir() == env["root"] / "run"
    assert run(env) == 0
    assert (env["root"] / "run" / "skill-sync" / "lock").is_file()


@pytest.mark.parametrize("value", [None, "relative/run"])
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_runtime_dir_falls_back_to_private_tmp_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str | None,
    platform: str,
) -> None:
    monkeypatch.setattr(ss.sys, "platform", platform)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    if value is None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    else:
        monkeypatch.setenv("XDG_RUNTIME_DIR", value)
    path = ss.runtime_dir()
    assert path.parent == tmp_path
    assert path.stat().st_mode & 0o777 == 0o700
    if platform == "darwin" and value is None:
        assert "XDG_RUNTIME_DIR" not in caplog.text
    else:
        assert "XDG_RUNTIME_DIR" in caplog.text


def test_macos_sync_without_xdg_runtime_dir_is_quiet(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ss.sys, "platform", "darwin")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(env["root"]))

    assert run(env) == 0
    lock = env["root"] / f"skill-sync-{os.getuid()}" / "skill-sync" / "lock"
    assert lock.is_file()
    assert "WARNING" not in caplog.text


def test_runtime_dir_rejects_unsafe_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    (tmp_path / f"skill-sync-{os.getuid()}").mkdir(mode=0o755)
    with pytest.raises(ss.SkillSyncError):
        ss.runtime_dir()


def test_watch_stops_on_interrupt(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    def stop(_: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", stop)
    assert run(env, "watch", "--interval", "1") == 0
    assert (env["agents"] / "aws-debug").is_symlink()


def test_dry_run_watch_preserves_links_and_skips_pull(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    env["agents"].mkdir(parents=True)
    stale = env["agents"] / "removed"
    stale.symlink_to(env["source"] / "removed")
    wrong = env["agents"] / "pr-review"
    wrong.symlink_to(env["source"] / "aws-debug")
    calls = fake_git(monkeypatch, {})

    def stop(_: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", stop)
    assert run(env, "--dry-run", "--pull", "watch") == 0
    assert not env["claude"].exists()
    assert not (env["agents"] / "aws-debug").exists()
    assert stale.is_symlink()
    assert wrong.readlink() == env["source"] / "aws-debug"
    assert calls == []


@pytest.mark.parametrize("interval", ["-1", "0", "nan", "inf", "-inf"])
def test_invalid_watch_interval_is_rejected_before_sync(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, interval: str
) -> None:
    calls = fake_git(monkeypatch, {})

    def unexpected_sleep(_: float) -> None:
        pytest.fail("invalid watch interval reached the sync loop")

    monkeypatch.setattr(time, "sleep", unexpected_sleep)
    assert run(env, "watch", "--pull", f"--interval={interval}") == 2
    assert not env["claude"].exists()
    assert not env["agents"].exists()
    assert calls == []


def test_conflict_exit_code_yields_to_drift(env: dict[str, Path]) -> None:
    blocker = env["claude"] / "pr-review"
    blocker.mkdir(parents=True)
    assert run(env, "status") == 1  # missing links outrank the conflict
    assert run(env) == ss.EXIT_CONFLICT
    assert blocker.is_dir()
    assert run(env, "status") == ss.EXIT_CONFLICT  # only the conflict remains
