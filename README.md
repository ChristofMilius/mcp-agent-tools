# mcp-agent-tools

Coordinating repository for a stack of MCP agent tools. Every tool is a git
**submodule**, pinned at the commit its code lives in — each keeps its own
remote and can be developed and pushed independently. This repo is only the
index; it contains no tool code itself.

## Stack

| Tool | Submodule | Purpose |
|---|---|---|
| mcp-agent-mail | `mcp_agent_mail` | Encrypted email + PGP MCP server — read/send/reply mail, contact book with key provenance, archive & search. Keys and bodies never reach the model context window. |
| mcp-agent-playwright | `mcp_agent_playwright` | Playwright browser automation MCP server — drives its own browser or attaches to a running Brave/Chrome over CDP; accessibility snapshots, click/type/evaluate. |

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