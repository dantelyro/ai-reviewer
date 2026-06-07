# ai-platform

Central hub for AI tooling. Provides automated, inline **Claude reviews of
GitLab merge requests**, with an always-on MCP server that lets Claude fetch
repository context on demand during a review.

See [CLAUDE.md](CLAUDE.md) for architecture and design decisions.

```
ai-platform/
├── mcp/
│   ├── server.py            # always-on MCP server (Streamable HTTP)
│   └── requirements.txt
├── ci/templates/
│   └── mr-review.yml        # reusable GitLab CI job, included by Node repos
├── scripts/
│   ├── review.py            # orchestrates the Claude review -> findings.json
│   ├── post-review.py       # posts findings as inline MR comments (stdlib only)
│   └── requirements.txt
├── prompts/
│   └── review-system-prompt.md
├── rules/
│   └── house-style.md       # edit: your team's house/business rules
├── CLAUDE.md
└── README.md
```

## 1. Run the MCP server (the dedicated machine)

The server reads the repositories under review. Each repo is a clone under
`REPOS_ROOT`; reads happen at the requested git ref via `git show`.

```bash
cd mcp
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

mkdir -p /srv/repos
git clone <node-repo-url> /srv/repos/diario-de-obras-front

REPOS_ROOT=/srv/repos \
MCP_AUTH_TOKEN="$(openssl rand -hex 32)" \
GIT_FETCH_ON_MISS=1 \
MCP_PORT=5000 \
python server.py
```

Put it behind TLS at a static domain so the Anthropic MCP connector can reach
it — it **must** be `https://` and publicly resolvable. The connector URL is
`https://<domain>/mcp`. Keep it running (systemd, a container, etc.) and keep
the cloned repos current enough to contain the commits being reviewed.

> Local development: Python is not installed on the original Windows dev box
> (only the Microsoft Store stub). Install Python 3.12 there, or develop on the
> Linux deploy target, before running the server.

### MCP tools exposed

| Tool | Purpose |
|------|---------|
| `get_file(repo, path, ref)` | Read a file at a git ref |
| `get_git_diff(repo, path, base, head)` | Unified diff for one file |
| `find_imports(repo, path, ref)` | List a JS/TS file's imported modules |
| `get_git_history(repo, path, limit, ref)` | Recent commits touching a file |
| `list_directory(repo, path, ref)` | List files/dirs at a path |

## 2. Configure each Node repo

Add the include to the repo's `.gitlab-ci.yml`:

```yaml
include:
  - project: 'your-group/ai-platform'
    file: 'ci/templates/mr-review.yml'
```

Set these **CI/CD variables** (Settings → CI/CD → Variables; mask secrets):

| Variable | Value |
|----------|-------|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `MCP_SERVER_URL` | `https://<domain>/mcp` |
| `MCP_AUTH_TOKEN` | same token the server expects |
| `GITLAB_TOKEN` | token with `api` scope (posts MR comments) |
| `AI_PLATFORM_REPO_URL` | clone URL of this repo, incl. an access token |

The job runs on `merge_request_event`, reviews the changed source files, and
posts inline comments. It is `allow_failure: true` so a review hiccup never
blocks a pipeline.

## 3. Customise the review

- Edit `rules/*.md` with your real house style and business rules.
- Tune `prompts/review-system-prompt.md` (severity rubric, what to flag).
- Add each Node repo's own `CLAUDE.md` — Claude fetches it via the MCP server
  for project-specific conventions.

## Local smoke test of the review script

With the MCP server reachable and inside a checkout of a Node repo:

```bash
pip install -r scripts/requirements.txt
export ANTHROPIC_API_KEY=... MCP_SERVER_URL=https://<domain>/mcp MCP_AUTH_TOKEN=...
export CI_PROJECT_NAME=diario-de-obras-front
BASE_SHA=<target-branch-sha> CI_COMMIT_SHA=$(git rev-parse HEAD) \
  python scripts/review.py
cat findings.json
```
