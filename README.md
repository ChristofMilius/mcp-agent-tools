# mcp-agent-tools

Coordinating repository for a stack of MCP agent tools. This repo is the
**index**, not a container: it holds none of the stack's tool code, and it does
not download or pin any. Each tool is an independent repository listed in
`stack.toml`, and you clone the ones you actually want. What lives here is the
manifest plus two standalone helpers that belong to no tool: a stdlib-only
launcher checker (`tools/launch_check.py`, tests in `tests/`) and an orphaned
opencode server reaper (`tools/reap_orphaned_opencode.ps1`).

Nothing in this repository can go stale. There is no recorded commit to fall
behind, no submodule to re-point, and nothing to bump.

## Stack

`stack.toml` is the single source of truth. This table is a reading of it.

| Tool | Clone as | Purpose |
|---|---|---|
| mcp-agent-docparser | `mcp_agent_docparser` | SDK documentation extractor MCP server — probe a docs site, save parsing receipts, emit clean timestamped .md files. |
| mcp-agent-mail | `mcp_agent_mail` | Encrypted email + PGP MCP server — read/send/reply mail, contact book with key provenance, archive & search. Keys and bodies never reach the model context window. |
| mcp-agent-openjev | `mcp_agent_openjev` | Local typed probabilistic decision service — Choice/Noul/Score with calibrated probabilities, as an MCP server + CLI. |
| mcp-agent-playwright | `mcp_agent_playwright` | Playwright browser automation MCP server — drives its own browser or attaches to a running Brave/Chrome over CDP. |
| mcp-agent-transcriber | `mcp_agent_transcriber` | Video transcription MCP server — grab a direct transcript from tube platforms or download audio and transcribe with local Whisper. |

## Launcher checker

`tools/launch_check.py` checks LM Studio MCP launch configurations before LM
Studio tries to spawn a server. It reads `stack.toml` for the tool inventory.
When a tool's checkout sits one level below this repository root — at the path
named by its `directory` entry — the checker also reads that tool's
`pyproject.toml` with the standard-library `tomllib` and treats it as
authoritative; otherwise it uses the launch details recorded in the manifest. It
therefore runs on a bare clone of this repository with no tool sources present,
reporting each absent tool as `inventory-source-absent` (informational). The
checker derives the console-script name and package from the script target; it
does not maintain a hardcoded tool list. It accepts both documented
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

The tests live in `tests/` at the repository root and use temporary directories
and fixture JSON only; they never read or write the real LM Studio
configuration:

```bash
python -m unittest
```

## Reaper

`tools/reap_orphaned_opencode.ps1` releases what opencode leaves behind when its
desktop app closes. The app spawns `opencode-cli.exe serve --service`; closing
the window does not stop that server, and every local MCP server the run booted
stays resident for as long as the machine is on. The watcher removes the
residue of a run that has already ended:

    app dies -> server is orphaned -> reaper kills the server
             -> each MCP child sees stdin EOF -> each exits cleanly

Only the orphaned server is ever signalled, and only when all of these hold:
its command line matches `serve --service`, its parent PID is no longer live,
it has been continuously orphaned for `-GraceSeconds` (30 s), and it is not the
watcher itself. The MCP children are never killed directly — they exit on
their own on EOF, and killing a launcher would orphan its interpreter instead.

Register it once at logon (no elevation required):

```powershell
$a = New-ScheduledTaskAction -Execute pwsh -Argument `
    "-NoProfile -WindowStyle Hidden -File `"<repo>\tools\reap_orphaned_opencode.ps1`""
$t = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName 'OpenCode-MCP-Reaper' -Action $a -Trigger $t `
    -Description 'Reap orphaned opencode servers so MCP processes are released'
```

Before registering — or after any change — run one pass by hand:

```powershell
pwsh -NoProfile -File .\tools\reap_orphaned_opencode.ps1 -Once -DryRun
```

`-DryRun` logs every decision and kills nothing. The log is append-only at
`%LOCALAPPDATA%\opencode-mcp-reaper\reaper.log`, one line per event
(`ORPHAN` / `REAP` / `REAPED`). A running instance never reloads the script
from disk, so restart the scheduled task after editing it.

## Stack manifest

**Add a tool** — push the tool's own repository to GitHub first, then add one
`[[tool]]` block to `stack.toml`:

```toml
[[tool]]
name = "mcp-agent-foo"
directory = "mcp_agent_foo"
repository = "https://github.com/<owner>/<repo>.git"
branch = "master"
project_name = "mcp-agent-foo"
script_name = "mcp-agent-foo"
target_module = "mcp_agent_foo:main"
purpose = "One line on what it does."
```

Then add the row to the table above. `directory`, `project_name`, `script_name`
and `target_module` are the required fields; `name`, `repository`, `branch` and
`purpose` are for humans. Copy `project_name` and `target_module` out of the
tool's own `pyproject.toml` so the two agree.

**Get a tool.** Clone it wherever you want it:

```bash
git clone https://github.com/ChristofMilius/mcp-agent-openjev.git
```

A tool does not need to live here at all — the manifest describes every tool
either way. For the checker to read a tool's own `pyproject.toml`, place that
checkout one level below this repository root, at the `directory` path from
`stack.toml`; otherwise the manifest is used.

**Update a tool.** Nothing to do here. The tool's own repository is the source
of truth for its code, and this manifest cannot fall behind it.

## Why a manifest and not submodules

This repository used to track each tool as a git submodule. That was replaced
because a submodule cannot do the one thing a coordinator wants: stay current
without ceremony.

A submodule entry in a git tree is a **gitlink** — mode `160000` holding one
commit SHA. Git has no way to record "this tracks a branch", so the tree always
names a commit and goes stale the moment the tool's `master` moves. Keeping it
current meant either remembering to bump five pointers by hand, or adding a
scheduled workflow to bump them for you, which then needed reviewing, and could
still record a pointer to a commit that had not been pushed yet.

A manifest holds no version, so there is nothing to be out of date. The costs
are real and worth stating plainly:

- `git clone` of this repository no longer gives you the stack. Clone the tools
  you want.
- There is no git-enforced "these five versions work together". If that matters,
  record it by hand in the table below.

### Last verified combination

Updated deliberately when you test the stack, not on every commit.

| Tool | Verified at |
|---|---|
| mcp-agent-docparser | _(not recorded)_ |
| mcp-agent-mail | _(not recorded)_ |
| mcp-agent-openjev | _(not recorded)_ |
| mcp-agent-playwright | _(not recorded)_ |
| mcp-agent-transcriber | _(not recorded)_ |