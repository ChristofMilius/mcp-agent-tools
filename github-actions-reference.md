# GitHub Actions — Reference & Reminder

The CI/CD machine built into GitHub. Push code, GitHub boots a fresh cloud VM,
runs your YAML-defined commands, and puts a green/red check on the commit.
Used here to lint + test this stack's tools on every push.

## Cost — the short version

**Public repos: free, unlimited. Always has been, still is (Jan 2026 changes didn't touch it).**
Private repos: 2,000 free minutes/month (GitHub Free), then billed (~$0.006/min Linux).
A 2-minute test run × dozens of pushes ≈ nothing. Do not fear the Meter.

## The 6 concepts that are everything

1. **Workflow** — one YAML file in `.github/workflows/`. One file = one workflow.
2. **`on:`** — triggers. `push`, `pull_request`, `workflow_dispatch` (manual button).
3. **`jobs:`** — parallel work units. Ours: one job `test` on `ubuntu-latest`.
4. **`steps:`** — sequential commands. `uses:` = a reusable **action** (someone's
   packaged step); `run:` = a raw shell line.
5. **Bare VM every run** — each run clones the repo, reinstalls everything, runs,
   and the VM dies. Nothing persists between runs. Don't expect state.
6. **The standard preamble** — `actions/checkout` (get the code) → set up uv/Python
   → run your stuff. Every workflow starts like this.

## The workflow we ship (mcp-agent-openjev)

```yaml
name: CI

on:
  push:
    branches: ["master"]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - uses: actions/setup-python@v5
        with:
          python-version-file: ".python-version"
      - run: uv sync --frozen
      - run: uv run ruff check .
      - run: uv run pytest -q
```

Line by line:
- `checkout@v4` — clone the repo onto the runner.
- `astral-sh/setup-uv@v6` — installs uv, puts it on PATH.
- `setup-python@v5` with `python-version-file` — reads `.python-version` so CI
  uses the exact same Python as local.
- `uv sync --frozen --all-extras` — installs the environment **exactly** from `uv.lock`
  (fails if the lock is stale instead of silently upgrading). `--all-extras` is
  **required**: this stack puts `ruff`/`pytest` in `[project.optional-dependencies]
  dev`, and uv does NOT install optional extras by default. Plain `uv sync`
  silently skips them → `uv run ruff` fails with "Failed to spawn: ruff".
- `uv run ruff check .` — lint (same as running it locally).
- `uv run pytest -q` — tests. The live-LM-Studio tests **auto-skip** on CI
  (no backend there); the 17 unit tests must pass.

Mirror of local: `uv run ruff check .` and `uv run pytest -q`.

## How to read the results

- Push → strip at top of the commit/PR shows ✅/❌, click "Details".
- GitHub → **Actions** tab → select the workflow → see each job's step log.
- Red check = your own log is in the job's step output. It always tells you why.

## Gotchas we hit (don't relearn these)

- **Whitelist gitignore**: this repo's `.gitignore` starts with `*`, so anything
  not explicitly un-ignored stays untracked. `.github/` MUST be whitelisted too,
  or the workflow file never reaches git → no CI, no error. Same class of bug as
  `README.md` not being tracked.
- **Live tests skip, not fail** — `test_live_lmstudio.py` uses `pytest.mark.skipif`
  on endpoint reachability, so CI is green without a local backend. Keep that
  shape for every live test you add, or CI will brick on the serverless runner.
- **`uv sync --frozen` over `uv sync`** — CI should reproduce, not innovate.
  And because dev tooling lives in an **optional extra** (`dev=`), CI must use
  `--all-extras` or ruff/pytest silently never install. Seen live: first CI run
  red in 13s on exactly this.
- **Actions versioning** — `@v4`/`@v5`/`@v6` pin a major; upgrades happen when you
  change the tag, not invisibly.
- **Master, not main** — this stack's repos use `master`; the `on.push.branches`
  filter matches that. Copy-paste from a main-based repo silently never triggers.
- **No GH Actions on private legacy-plan repos** — irrelevant here (public).

## House rules for this stack

- Every tool repo got the same CI when it was onboarded. If a tool lacks
  `.github/workflows/ci.yml`, its workflow file is missing or `.github/` isn't
  whitelisted in its `.gitignore`.
- Submodule tip: the coordinator repo pins tools at commits — CI lives in each
  tool repo, not in the coordinator.