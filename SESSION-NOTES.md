# Session Notes — ai-platform scaffold (2026-06-07)

Portable record of this Claude Code session so work can resume on another
machine (next: Fedora). Open this and say: "Read SESSION-NOTES.md and continue."

## TL;DR

Scaffolded the **ai-platform** repo from the AI handoff: a Python MCP server +
GitLab CI integration that runs automated, inline Claude reviews on merge
requests. All files written; **nothing has been executed yet** because this
Windows box has no real Python (only the Microsoft Store stub). Next: move to
Fedora, run/test, deploy the MCP server.

> Repo dir is currently named `ai-review`; the design calls it `ai-platform`.
> Same project — rename when convenient.

## Where this came from

A previous "AI-HANDOFF" defined the architecture (pasted at session start, not
committed). Key prior decisions carried in:

- GitLab native MCP is a paid feature → dead end on Free plan (self-hosted
  GitLab 18.11.4-ee, license null).
- Tooling layer is **Python + shell only** (no Node.js, no `.mjs`).
- Central **ai-platform** repo is the hub; Node repos include its CI template.
- MCP server is **always-on (Option A)** on a dedicated machine with a static
  https domain; CI connects at review time.
- Context strategy: send **full changed files with changed lines marked**
  (not raw diffs); Claude fetches dependencies on demand via MCP tools.
- **Inline comments only**, posted as GitLab discussions with position SHAs.

## What was built this session

```
ai-platform/
├── mcp/
│   ├── server.py            # always-on MCP server (FastMCP, Streamable HTTP)
│   └── requirements.txt     # mcp, uvicorn, starlette
├── ci/templates/
│   └── mr-review.yml        # reusable GitLab CI job (include: from Node repos)
├── scripts/
│   ├── review.py            # orchestrates the Claude review -> findings.json
│   ├── post-review.py       # posts inline MR comments (stdlib only, 0 deps)
│   └── requirements.txt     # anthropic
├── prompts/
│   └── review-system-prompt.md   # reviewer role, severity rubric, JSON contract
├── rules/
│   └── house-style.md       # STARTER house/business rules — needs real content
├── CLAUDE.md                # architecture + conventions (project context)
├── README.md                # deploy/use guide
├── SESSION-NOTES.md         # this file
├── .env.example
└── .gitignore
```

### MCP tools exposed by `mcp/server.py`

| Tool | Purpose |
|------|---------|
| `get_file(repo, path, ref)` | Read a file at a git ref |
| `get_git_diff(repo, path, base, head)` | Unified diff for one file |
| `find_imports(repo, path, ref)` | List a JS/TS file's imported modules |
| `get_git_history(repo, path, limit, ref)` | Recent commits touching a file |
| `list_directory(repo, path, ref)` | List files/dirs at a path |

## Verified API facts (don't re-derive)

- **MCP connector** (Messages API): pass `mcp_servers=[{type:"url", url, name,
  authorization_token}]` + `tools=[{type:"mcp_toolset", mcp_server_name}]`,
  beta header **`mcp-client-2025-11-20`**, via `client.beta.messages.create`.
- Connector server URL **must be `https://` and publicly reachable** — Anthropic
  calls it from their side. `authorization_token` arrives as `Authorization:
  Bearer <token>`.
- Response carries `mcp_tool_use` / `mcp_tool_result` blocks; server-tool loops
  can return `stop_reason: "pause_turn"` (review.py resumes on it).
- Model: **`claude-opus-4-8`**, `thinking={"type":"adaptive"}`,
  `output_config={"effort":"high"}`. Do NOT use `budget_tokens` /
  `temperature` (removed on Opus 4.7+).

## Decisions / deviations from the handoff

1. **Git-ref-aware reads.** MCP tools read via `git show <ref>:<path>`, not the
   working tree, so Claude sees files exactly at the MR commit. All tools take a
   `ref`; review.py passes the head SHA. (Closes a consistency hole in the
   original "reads from disk" idea.)
2. **Added `scripts/review.py`** as the orchestrator the handoff implied (its
   workflow step 5 calls Claude). Split from post-review.py so the GitLab poster
   stays stdlib-only.
3. **"CLAUDE.md per review" → `prompts/` + `rules/`.** Cross-repo guidance ships
   always; each Node repo's own CLAUDE.md is fetched on demand via MCP.
   ai-platform's own CLAUDE.md documents ai-platform.
4. **CI uses stock `python:3.12-slim`** (installs git). Read "no Docker image
   with a specific runtime" as "don't maintain a custom image." Revisit if that
   was meant literally.
5. **Added `list_directory`** MCP tool beyond the handoff's four — agents need
   to discover files before fetching them.
6. **Bearer-token auth** on the MCP server via `MCP_AUTH_TOKEN`.

## NOT done yet

- [ ] **Run / syntax-check anything** — blocked: no real Python on this box
  (MS Store stub only). First task on Fedora.
- [ ] Fill `rules/` with real house + business rules.
- [ ] Deploy the MCP server to the dedicated machine (https + token + systemd).
- [ ] Set CI/CD variables in each Node repo (see README §2).
- [ ] End-to-end test on a real MR.
- [ ] Rename repo/dir to `ai-platform`.

## How to resume on Fedora

```bash
# 1. Get the code (after this push)
git clone <github-url> ai-platform && cd ai-platform

# 2. MCP server
cd mcp
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
mkdir -p /srv/repos
git clone <node-repo-url> /srv/repos/diario-de-obras-front
REPOS_ROOT=/srv/repos MCP_AUTH_TOKEN="$(openssl rand -hex 32)" \
  GIT_FETCH_ON_MISS=1 MCP_PORT=5000 python server.py
# put behind TLS at a static domain; connector URL = https://<domain>/mcp

# 3. Smoke-test the review script (inside a Node repo checkout)
pip install -r scripts/requirements.txt
export ANTHROPIC_API_KEY=... MCP_SERVER_URL=https://<domain>/mcp MCP_AUTH_TOKEN=...
export CI_PROJECT_NAME=diario-de-obras-front
BASE_SHA=<target-sha> CI_COMMIT_SHA=$(git rev-parse HEAD) python scripts/review.py
cat findings.json
```

Recommended on Fedora: run `server.py` under **systemd** as the always-on
service; keep the cloned repos current enough to contain reviewed commits.

## Environment note

This Windows box: `python`/`python3` resolve only to the MS Store
execution-alias stub — no interpreter, no pip. `git` works. Develop on Fedora
(or WSL2) for dev/prod parity with the Linux deploy target.
