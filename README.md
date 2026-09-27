# mcp-agent-tools

Coordinating repository for a stack of MCP agent tools. Every tool is a git
**submodule** that declares `branch = master`, so the stack can be advanced to
each tool's `master` tip with one `--remote` call — each keeps its own remote
and can be developed and pushed independently. This repo is the index;
it contains no tool code itself, plus a small stdlib-only launcher checker and
its tests under `tools/`.

## Stack

| Tool | Submodule | Purpose |
|---|---|---|
| mcp-agent-mail | `mcp_agent_mail` | Encrypted email + PGP MCP server — read/send/reply mail, contact book with key provenance, archive & search. Keys and bodies never reach the model context window. |
| mcp-agent-playwright | `mcp_agent_playwright` | Playwright browser automation MCP server — drives its own browser or attaches to a running Brave/Chrome over CDP; accessibility snapshots, click/type/evaluate. |
| mcp-agent-docparser | `mcp_agent_docparser` | SDK documentation extractor MCP server — probe a docs site, save parsing receipts, emit clean timestamped .md files via doc_parse tools. |
| mcp-agent-transcriber | `mcp_agent_transcriber` | Video transcription MCP server — grab a direct transcript from tube platforms or download audio and transcribe with local Whisper. |
| mcp-agent-openjev | `mcp_agent_openjev` | Local typed probabilistic decision service — Choice/Noul/Score with calibrated probabilities over OpenJev + LM Studio logprobs, as an MCP server + CLI. |

## Launcher checker

`tools/launch_check.py` checks LM Studio MCP launch configurations before LM
Studio tries to spawn a server. It reads `.gitmodules` for the tool inventory,
then reads each tool's `pyproject.toml` with the standard-library `tomllib`.
The checker derives the console-script name and package from the script target;
it does not maintain a hardcoded tool list. It accepts both documented
`uv --project <dir> run python -m <package> [subcommand]` and
`uv --project <dir> run <console-script> [subcommand]` forms when their
preconditions exist. Configured subcommands are verified against the server's
own `--help` output.

LM Studio app configs are discovered below the user's home directory. Both
`mcpServers` and the current `servers` list key are accepted. The checker also
validates enabled state, stdio transport, project and working directories,
known names, duplicate entries, malformed JSON, and small launch-argument
typos. A tool that is not configured in an app is informational, not a
failure. Valid module/script alternatives and equivalent path spellings are
reported as informational rather than rewritten.

Run it from the repository root with no environment or dependencies:

```bash
python tools/launch_check.py
python tools/launch_check.py --diff
python tools/launch_check.py --app bionic
python tools/launch_check.py --json
python tools/launch_check.py --log-file logs/launch_check.log
```

The command exits with status 1 when any `FAIL` is present. `--diff` prints a
unified diff and never writes. `--apply` is explicit, writes only unambiguous
near-match launch repairs atomically, and logs every change; it never invents or
changes a server UUID, adds a missing entry, or rewrites an unknown/ambiguous
configuration. `logs/` is ignored by the whitelist `.gitignore`.

The tests use temporary directories and fixture JSON only; they never read or
write the real LM Studio configuration:

```bash
python -m unittest
```

## Tool lifecycle

**Add a tool** (it must already be a repo pushed to GitHub):

```bash
git submodule add https://github.com/<owner>/<repo>.git <path>
git commit -m "stack: add <tool> as submodule"
```

**Clone the stack** (gets every tool at the commit this repo records — see
[Pinning](#pinning) for why that is not automatically the `master` tip):

```bash
git clone https://github.com/ChristofMilius/mcp-agent-tools.git
git submodule update --init --recursive
```

**Advance the stack to each tool's `master` tip** — normally you do nothing,
the [bump workflow](#bumping) opens a pull request for you. To do it by hand
after pushing commits in the child repos:

```bash
git submodule update --init --remote --recursive
git add -A
git status --short        # review every moved pointer before committing
git commit -m "stack: bump tools to master"
```

> `--remote` moves every submodule it can reach, so a busy child repo will show
> up in the same commit as an unrelated one. Use
> `git submodule update --init --remote <path>` to advance one tool at a time.
>
> `--init` is not optional here. On a checkout where the submodules were never
> cloned, plain `--remote` silently does nothing and still exits 0.

## Bumping

`.github/workflows/bump-submodules.yml` runs every six hours and on demand
from the Actions tab. It advances all five pointers to their `master` tips and,
if anything moved, opens (or refreshes) a pull request titled
`stack: bump tools to their master tip`. Review the submodule diff and merge.

The child repos are public, so the job clones them over HTTPS with no secret
and no deploy key — it rewrites the SSH URLs in `.gitmodules` to HTTPS at
runtime rather than changing them on disk.

The bot only ever sees **pushed** commits. A tool with unpushed work on
`master` looks unchanged to it.

## Pinning

A submodule entry in this repo's tree is a **gitlink**: mode `160000` holding
one commit SHA. Git has no representation for "this submodule tracks a branch",
so the tree always names a commit.

`branch = master` in `.gitmodules` therefore does not make a fresh clone follow
`master`. `git submodule update --init` reads the SHA from the index and checks
out exactly that commit. What `branch = master` changes is the resolution of
`--remote`: it makes `git submodule update --remote` fetch and check out
`origin/master` for that submodule instead of the remote's default `HEAD`, so
the declared tracking intent is explicit and identical for all five tools
rather than depending on whatever each remote's `HEAD` happens to point at.

The practical consequence: **the stack only reflects the newest child commits
after a bump lands here.** That is at most one workflow interval, and you can
force it at any time with the manual trigger. The recorded SHA is what makes
`clone` + `submodule update` reproduce a known-good stack rather than whatever
happened to be on `master` today.