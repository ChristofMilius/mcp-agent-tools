# mcp-agent-tools

Coordinating repository for a stack of MCP agent tools. Every tool is a git
**submodule**, pinned at the commit its code lives in — each keeps its own
remote and can be developed and pushed independently. This repo is the index;
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

**Clone the stack** (gets every pinned tool at the recorded commit):

```bash
git clone https://github.com/ChristofMilius/mcp-agent-tools.git
git submodule update --init --recursive
```

**Update a pinned tool** after pushing new commits in its own repo:

```bash
cd <path> && git pull origin master
cd .. && git add <path> && git commit -m "stack: bump <tool>"
```

> The parent repo pins each tool at a commit. Updating a tool's pointer here
> is a deliberate, separate commit — no tool is ever silently moved.