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
      - run: uv sync --locked --all-extras
      - run: uv run ruff check .
      - run: uv run pytest -q
```

Line by line:
- `checkout@v4` — clone the repo onto the runner.
- `astral-sh/setup-uv@v6` — installs uv, puts it on PATH.
- `setup-python@v5` with `python-version-file` — reads `.python-version` so CI
  uses the exact same Python as local.
- `uv sync --locked --all-extras` — installs the environment **exactly** from
  `uv.lock`, and **fails if the lock disagrees with `pyproject.toml`** instead of
  installing from a stale lock. `--all-extras` is
  **required**: this stack puts `ruff`/`pytest` in `[project.optional-dependencies]
  dev`, and uv does NOT install optional extras by default. Plain `uv sync`
  silently skips them → `uv run ruff` fails with "Failed to spawn: ruff".
- `uv run ruff check .` — lint (same as running it locally).
- `uv run pytest -q` — tests. The live-LM-Studio tests **auto-skip** when there
  is no backend reachable, which is the case on CI. A healthy run is
  `13 passed, 3 skipped` — check the counts, not just the exit code, or a suite
  that has silently lost backend coverage still looks green.

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
- **`uv sync --locked`, not `--frozen`** — CI should reproduce, not innovate.
  And because dev tooling lives in an **optional extra** (`dev=`), CI must use
  `--all-extras` or ruff/pytest silently never install. Seen live: first CI run
  red in 13s on exactly this.
  - **`--frozen` does NOT check the lock is current.** It installs from
    `uv.lock` whatever it says and never looks at `pyproject.toml`. `--locked`
    is the flag that fails when the two disagree. This was believed backwards
    here for months: `openjevpro` stayed in `mcp-agent-openjev`'s lock after
    `b579b51` removed it from the dependencies, `uv lock --check` failed on a
    clean checkout of `master`, and CI stayed green the whole time. Verified
    side by side on a deliberately un-locked dependency: `--frozen` exits 0,
    `--locked` exits 1.
- **`--remote` needs `--init` or it silently no-ops** — on a checkout whose
  submodules were never cloned, `git submodule update --remote` moves nothing
  and **still exits 0**. `actions/checkout` does not initialise submodules
  unless you pass `submodules:`. Use `--init --remote`. Seen live, same day as
  the `--locked` finding above.
- **Scheduled workflows are disabled after 60 days without repo activity** — a
  `schedule:` job on an idle repo stops firing with no error and no failed run.
  If one ever goes quiet, check this before debugging the job itself.
- **Cloning a public repo in CI needs no secret** — for SSH submodule URLs,
  rewrite them at runtime with
  `git config --global url."https://github.com/".insteadOf "git@github.com:"`.
  No deploy key, no PAT, and the committed URLs stay SSH for local work. A
  private repo would need a deploy key or a token instead.
- **Actions versioning** — `@v4`/`@v5`/`@v6` pin a major; upgrades happen when you
  change the tag, not invisibly.
- **Master, not main** — this stack's repos use `master`; the `on.push.branches`
  filter matches that. Copy-paste from a main-based repo silently never triggers.
- **No GH Actions on private legacy-plan repos** — irrelevant here (public).

## House rules for this stack

- Every tool repo got the same CI when it was onboarded. If a tool lacks
  `.github/workflows/ci.yml`, its workflow file is missing or `.github/` isn't
  whitelisted in its `.gitignore`.
- Lint/test CI lives in each **tool** repo. The coordinator has no code to test
  and no workflows: it holds `stack.toml`, a manifest, not a container. See the
  coordinator README's *Why a manifest and not submodules* for the reasoning.