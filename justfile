set shell := ["bash", "-euo", "pipefail", "-c"]

# List recipes
default:
    @just --list

# Install dev dependencies
sync:
    uv sync

# Run the tests (extra args go to pytest, e.g. `just test -k conflict`)
test *args:
    uv run pytest {{ args }}

# Lint (ruff + rumdl)
lint:
    uv run ruff check .
    uv run rumdl check .

# Check formatting without changing files
fmt-check:
    uv run ruff format --check .

# Apply safe lint fixes (imports etc.), then format
fmt:
    uv run ruff check --select I --fix .
    uv run ruff format .

# Type check with pyrefly
types:
    uv run pyrefly check

# Everything CI would run
check: lint fmt-check types test

# Dry run against the example config
dry-run:
    ./skill_sync.py -n -c config.example.toml sync

# Show drift using the user's real config (exit 1 drift, 3 conflicts only)
status:
    ./skill_sync.py status

# Sync for real using the user's real config
apply:
    ./skill_sync.py sync

# Refresh uv.lock
lock:
    uv lock

# Remove caches and bytecode
clean:
    rm -rf .ruff_cache .pytest_cache .pyrefly_cache .rumdl_cache .mypy_cache .hypothesis .coverage htmlcov
    find . -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
