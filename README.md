# skill-sync

Keep every agent's skills folder in sync with one central folder.

`skill-sync` keeps one symlink per skill in each agent's skills folder
(`~/.claude/skills`, `~/.kiro/skills`, `~/.agents/skills`, and so on), all pointing
into a single source folder. A skill is a directory that contains a `SKILL.md`.
Add, rename, or delete a skill in the source, run `skill-sync`, and every agent
sees the change.

It is a single Python file. It needs Python 3.12 or later and nothing else.

## Why per-skill links

Linking whole folders is simpler, but some agents (Claude Code) have not reliably
found skills through a folder-level symlink. None of them scan more than one level
deep into a linked bundle. One symlink per skill works everywhere.

## Install

```bash
install -m 755 skill_sync.py ~/.local/bin/skill-sync
```

The file starts with a `uv run --script` shebang, so `uv` runs it with a suitable
Python. Without `uv`, run it with `python3 skill-sync` (3.12 or later).

## Quick start

1. Create `~/.config/skill-sync/config.toml`. Start from
   [`config.example.toml`](config.example.toml):

   ```toml
   source = "~/.local/agents/skills"
   targets = ["~/.claude/skills", "~/.kiro/skills", "~/.agents/skills"]
   pull = true
   ```

2. Preview, then apply:

   ```bash
   skill-sync -n sync   # dry run: print what would change
   skill-sync sync      # create, repair and prune links
   ```

## Commands

| Command    | What it does                                                     |
| ---------- | ---------------------------------------------------------------- |
| `sync`     | Create, repair and prune links. This is the default command.     |
| `status`   | Show drift without changing anything.                            |
| `validate` | Check each `SKILL.md` frontmatter against the Agent Skills spec. |
| `watch`    | Run `sync` every `--interval` seconds (default 60) until stopped. |

### Options

| Option                         | Meaning                                                        |
| ------------------------------ | -------------------------------------------------------------- |
| `-c, --config FILE`            | Use this config file instead of the default.                   |
| `-s, --source DIR`             | Central folder that holds `<skill>/SKILL.md`.                  |
| `-t, --target DIR`             | Agent skills folder. Repeat for several.                       |
| `--pull / --no-pull`           | Run `git pull --ff-only` in the source before syncing.         |
| `--relative / --no-relative`   | Write relative symlinks instead of absolute ones.              |
| `--strict / --no-strict`       | Do not link skills that fail validation, and prune their links. |
| `-n, --dry-run`                | Show changes only.                                             |
| `--interval SECONDS`           | Time between passes for `watch`.                               |
| `-v, --verbose` / `-q, --quiet` | Also show unchanged links / show only warnings and errors.     |

## Configuration

Settings come from three places. A later one overrides an earlier one:
built-in defaults, then `$XDG_CONFIG_HOME/skill-sync/config.toml`
(`~/.config/skill-sync/config.toml` if unset), then command-line flags.

| Key        | Type            | Default                                                     |
| ---------- | --------------- | ----------------------------------------------------------- |
| `source`   | string          | none (required, from the file or `--source`)                |
| `targets`  | list of strings | `~/.agents/skills`                                         |
| `pull`     | boolean         | `false`                                                     |
| `relative` | boolean         | `false`                                                     |
| `strict`   | boolean         | `false`                                                     |

Paths may use `~` and `$VARS`. Unknown keys and wrong types are errors.

`~/.agents/skills` is read by Codex, pi, Gemini CLI, and Copilot CLI. Do not also
list their own folders, or those agents see every skill twice.

## What it will and will not touch

A link is **managed** only if it is a symlink that points into the source folder.

- Managed links are created, repaired, and removed when their skill disappears.
  This includes dangling links and leftover `.skill-sync-tmp` links.
- Real files, real directories, and symlinks that point elsewhere are never changed
  or deleted. They are reported as `conflict` so you can clean them up yourself.
- If a target folder is itself a symlink to the whole source folder, it is skipped
  with a warning. Replace it with a real directory so per-skill links can exist.
- Links are swapped atomically (a temporary link is renamed over the real name),
  so an agent never sees a missing skill during a sync.
- An unmanaged file, directory, or symlink at the temporary path is reported as
  a conflict and preserved. Managed temporary links are pruned before link swaps.

Example output:

```text
create   ~/.claude/skills/my-skill -> /home/me/.local/agents/skills/my-skill
prune    ~/.kiro/skills/old-skill  (no longer in source)
conflict ~/.agents/skills/other  (real file or directory in the way)
```

## Validation

`validate` (and every `sync`, as warnings) checks each skill:

- `name` is present, matches the folder name, uses lowercase letters, digits and
  single hyphens, and is at most 64 characters.
- `description` is present and at most 1024 characters.

The frontmatter reader is a small scalar and block-scalar parser, not a full YAML
parser. This keeps the script free of dependencies.

## Git pull

With `pull = true`, `sync` runs `git pull --ff-only` in the source first.

- If the source is not in a git work tree, or its branch has no upstream, the pull
  is skipped. Local-only skills are a normal setup.
- If a pull fails or times out (120 seconds), you get a warning and the sync
  continues with the files on disk.
- Git never prompts for credentials, so unattended runs cannot hang.

## Exit codes

| Code | Meaning                                                              |
| ---- | -------------------------------------------------------------------- |
| 0    | Success, or everything is in sync.                                   |
| 1    | Drift (`status`), invalid skills (`validate`), or failed actions.    |
| 2    | Configuration or usage error.                                        |
| 3    | Only conflicts remain: an unmanaged path is in the way.              |

When both apply, 1 takes precedence over 3.

## Running automatically

[`contrib/`](contrib) has ready-made units. They expect the script at
`~/.local/bin/skill-sync`.

### systemd (user units)

```bash
cp contrib/skill-sync.{service,timer,path} ~/.config/systemd/user/
# Edit PathChanged= in skill-sync.path to match your `source`.
systemctl --user enable --now skill-sync.timer skill-sync.path
```

The timer syncs 2 minutes after boot and every 15 minutes after that. The path
unit syncs when a skill is added, removed, or renamed in the source.

### macOS (launchd)

Use `contrib/com.github.thimslugga.skill-sync.plist`. Edit `WatchPaths` to match
your `source`.

### Anywhere else

```bash
skill-sync watch --interval 60
```

The interval must be finite and greater than zero. Use `--dry-run watch` to
preview each pass without pulling or changing skill links.

A file lock in `$XDG_RUNTIME_DIR/skill-sync/lock` stops a timer run and `watch`
from syncing at the same time. Without an absolute `$XDG_RUNTIME_DIR`, the lock
lives under a private `skill-sync-<uid>` directory in the system temporary
folder. This is the normal default on macOS and needs no environment setup.
Other platforms warn when using this fallback; relative values warn everywhere.

## Development

The project uses [`uv`](https://docs.astral.sh/uv/) and [`just`](https://just.systems/).

```bash
uv sync                  # install dev dependencies
uv run pytest            # tests
uv run ruff check . && uv run ruff format --check .
uv run pyrefly check     # type check
just check               # lint, format check, types and tests together
just dry-run             # dry run against config.example.toml
```

Tests are in `tests/test_skill_sync.py`. They call `main([...])` against a
temporary directory with `HOME` and `XDG_*` redirected, and fake `git` by
patching `subprocess.run`.

The script must stay stdlib-only. Do not add runtime dependencies.
