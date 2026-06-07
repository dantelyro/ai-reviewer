# ai-platform

Central hub for AI tooling across the team's repositories. Today it powers
automated, inline Claude reviews of GitLab merge requests; it is the one place
review behaviour, prompts, and rules are maintained.

> Repo directory note: this repo currently lives in a working directory named
> `ai-review`. The handoff design calls it `ai-platform`; treat the two names
> as the same project. Rename the directory/remote to `ai-platform` when
> convenient.

## How a review works

```
1. MR opened/updated in a Node repo (e.g. diario-de-obras-front)
2. GitLab runs the pipeline on merge_request_event
3. The repo includes ci/templates/mr-review.yml from this repo
4. The job clones ai-platform and runs scripts/review.py:
     - computes changed source files (git diff base..head)
     - sends each file's full content with changed lines marked, plus the
       review guidelines + house rules, to Claude (Messages API)
     - connects Claude to the MCP server so it can fetch dependencies on demand
     - Claude returns a JSON array of findings
5. scripts/post-review.py posts each finding as an inline MR comment
```

## Components

- **`mcp/server.py`** — always-on MCP server (Streamable HTTP). Exposes
  read-only, git-ref-aware tools over the repositories under review:
  `get_file`, `get_git_diff`, `find_imports`, `get_git_history`,
  `list_directory`. Consumed by the Anthropic Messages API MCP connector
  (`mcp_servers`, beta `mcp-client-2025-11-20`). Deployed once on a dedicated
  machine with a static **https** domain (Anthropic calls it from their side).
- **`scripts/review.py`** — the review orchestrator. Uses the official
  `anthropic` SDK. Model `claude-opus-4-8`, adaptive thinking, effort `high`.
- **`scripts/post-review.py`** — posts findings to GitLab via the discussions
  API. Standard library only (zero pip deps).
- **`ci/templates/mr-review.yml`** — the reusable CI job Node repos `include:`.
- **`prompts/review-system-prompt.md`** — the reviewer system prompt (role,
  severity rubric, JSON output contract).
- **`rules/*.md`** — house style and business rules, concatenated into every
  review. **Starter content — edit for your team.**

### Where "context per review" comes from

The handoff describes sending Claude a `CLAUDE.md` per review. In this layout
that content is split for clarity: cross-repo review guidance lives in
`prompts/` + `rules/` (always sent), and each Node repo's own project-specific
conventions live in *that* repo's `CLAUDE.md`, which Claude fetches on demand
via the MCP `get_file` tool. This file (ai-platform's CLAUDE.md) documents
ai-platform itself.

## Key design decisions

- **Python + shell only.** No Node.js in the tooling layer.
- **Full files, changed lines marked** (`<<< CHANGED START/END >>>`), not raw
  diffs — Claude needs surrounding context to judge correctness.
- **Git-ref-aware reads.** The MCP tools read via `git show <ref>:<path>`, so
  Claude sees files exactly as they are at the MR commit, not the server's
  working tree. Always pass the MR head SHA as `ref`.
- **Report everything, filter downstream.** The prompt asks Claude to surface
  all findings with severity + confidence; the human reading the MR triages.
- **Inline comments only**, anchored with diff position SHAs; unanchored notes
  are the fallback so nothing is dropped.

## Conventions for working in this repo

- Python ≥ 3.10. Keep `post-review.py` dependency-free (stdlib only) so CI
  needs nothing but Python.
- Use the official `anthropic` SDK for Claude calls — never hand-rolled HTTP.
- Model defaults: `claude-opus-4-8`, `thinking={"type":"adaptive"}`,
  `output_config={"effort":"high"}`. Don't add `budget_tokens` /
  `temperature` (removed on Opus 4.7+).
- Pin the MCP connector beta header to `mcp-client-2025-11-20`.

## Operational notes

- The MCP server needs each reviewed repo cloned under `REPOS_ROOT`, kept
  current enough to contain the MR's head commit. Set `GIT_FETCH_ON_MISS=1` to
  let it fetch once when a ref is missing.
- Set `MCP_AUTH_TOKEN` on the server and the same value as the CI
  `MCP_AUTH_TOKEN` variable; it is passed as a bearer token by the connector.

## Status / TODO

- [x] MCP server (`mcp/server.py`) + deps
- [x] CI template (`ci/templates/mr-review.yml`)
- [x] Review orchestrator (`scripts/review.py`)
- [x] GitLab poster (`scripts/post-review.py`)
- [x] Prompts + starter rules + this CLAUDE.md
- [ ] Fill `rules/` with real house/business rules
- [ ] Deploy the MCP server to the dedicated machine (https + token)
- [ ] Set CI/CD variables in each Node repo (see README)
- [ ] End-to-end test on a real MR
