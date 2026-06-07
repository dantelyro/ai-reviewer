"""ai-platform MCP server.

An always-on MCP server (Streamable HTTP) that gives Claude on-demand,
read-only access to the source repositories under review. It is consumed by the
Anthropic Messages API "MCP connector" (`mcp_servers`) during a merge-request
review: Claude calls these tools mid-review to fetch only the context it needs
(a dependency's source, a file's diff, its imports, its history) instead of
receiving the whole codebase up front.

Design notes
------------
* All file reads go through git (`git show <ref>:<path>`) rather than the
  working tree, so Claude always sees the file exactly as it is at the MR's
  commit SHA -- not whatever happens to be checked out on disk. Pass the MR
  head SHA as ``ref`` for a consistent view.
* Every repository lives as a clone/worktree directly under ``REPOS_ROOT``.
  The ``repo`` argument is a bare directory name; path traversal is rejected.
* The Anthropic connector requires an ``https://`` URL and sends the
  ``authorization_token`` as ``Authorization: Bearer <token>``. When
  ``MCP_AUTH_TOKEN`` is set, every request is checked against it.

Environment
-----------
MCP_HOST          bind host (default 0.0.0.0)
MCP_PORT          bind port (default 5000)
MCP_PATH          Streamable HTTP path (default /mcp)
REPOS_ROOT        directory containing the cloned repos (default ./repos)
MCP_AUTH_TOKEN    shared bearer token; if unset, auth is disabled (dev only)
GIT_FETCH_ON_MISS if "1", run `git fetch` once when a ref is missing (default 0)

Run locally:
    pip install -r requirements.txt
    REPOS_ROOT=/srv/repos MCP_AUTH_TOKEN=dev-token python server.py
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

REPOS_ROOT = Path(os.environ.get("REPOS_ROOT", "./repos")).resolve()
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()
GIT_FETCH_ON_MISS = os.environ.get("GIT_FETCH_ON_MISS", "0") == "1"
GIT_TIMEOUT = 30  # seconds per git invocation

mcp = FastMCP(
    name="ai-platform",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "5000")),
    streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
)


# --------------------------------------------------------------------------- #
# Errors and validation
# --------------------------------------------------------------------------- #


class ToolError(Exception):
    """Raised for invalid input or git failures; surfaced to Claude as text."""


def _resolve_repo(repo: str) -> Path:
    """Validate ``repo`` and return its absolute path under REPOS_ROOT."""
    if not repo or "/" in repo or "\\" in repo or repo in (".", ".."):
        raise ToolError(f"Invalid repo name: {repo!r}")
    path = (REPOS_ROOT / repo).resolve()
    # Guard against traversal via symlinks or odd names.
    if path.parent != REPOS_ROOT:
        raise ToolError(f"Invalid repo name: {repo!r}")
    if not (path / ".git").exists():
        raise ToolError(
            f"Repo {repo!r} not found under REPOS_ROOT. "
            f"Available: {', '.join(_list_repos()) or '(none)'}"
        )
    return path


def _safe_relpath(path: str) -> str:
    """Validate a repo-relative path; reject absolute paths and traversal."""
    norm = path.strip().replace("\\", "/").lstrip("./")
    if not norm:
        raise ToolError("Empty path")
    if norm.startswith("/") or ".." in norm.split("/"):
        raise ToolError(f"Invalid path: {path!r}")
    return norm


def _list_repos() -> list[str]:
    if not REPOS_ROOT.exists():
        return []
    return sorted(
        p.name for p in REPOS_ROOT.iterdir() if (p / ".git").exists()
    )


def _git(repo_path: Path, *args: str) -> str:
    """Run a git command in ``repo_path`` and return stdout, raising on error."""
    proc = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )
    if proc.returncode != 0:
        raise ToolError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def _ensure_ref(repo_path: Path, ref: str) -> None:
    """Optionally fetch once if ``ref`` is not present locally."""
    if not GIT_FETCH_ON_MISS:
        return
    check = subprocess.run(
        ["git", "-C", str(repo_path), "cat-file", "-e", f"{ref}^{{commit}}"],
        capture_output=True,
        timeout=GIT_TIMEOUT,
    )
    if check.returncode != 0:
        subprocess.run(
            ["git", "-C", str(repo_path), "fetch", "--quiet", "--all"],
            capture_output=True,
            timeout=GIT_TIMEOUT * 4,
        )


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@mcp.tool()
def get_file(repo: str, path: str, ref: str = "HEAD") -> str:
    """Read the full contents of a file from a repository at a given git ref.

    Call this when reviewing changed code and you need to see a dependency, a
    sibling module, a type definition, or any file that is not in the diff.
    Reads the file as it exists at ``ref`` (pass the MR head commit SHA for a
    view consistent with the change under review).

    Args:
        repo: Repository directory name (e.g. "diario-de-obras-front").
        path: Repo-relative path (e.g. "src/components/Foo.tsx").
        ref: Commit SHA, branch, or tag. Defaults to "HEAD".
    """
    repo_path = _resolve_repo(repo)
    rel = _safe_relpath(path)
    _ensure_ref(repo_path, ref)
    return _git(repo_path, "show", f"{ref}:{rel}")


@mcp.tool()
def get_git_diff(repo: str, path: str, base: str = "", head: str = "HEAD") -> str:
    """Return the unified git diff for a single file between two refs.

    Call this when you need to know precisely what changed in a file -- the
    added/removed lines with surrounding context -- rather than its full
    contents. If ``base`` is omitted, diffs ``head`` against its first parent.

    Args:
        repo: Repository directory name.
        path: Repo-relative path to the file.
        base: Base ref (e.g. the MR target-branch SHA). Empty = head's parent.
        head: Head ref. Defaults to "HEAD".
    """
    repo_path = _resolve_repo(repo)
    rel = _safe_relpath(path)
    _ensure_ref(repo_path, head)
    range_ = f"{base}..{head}" if base else f"{head}~1..{head}"
    return _git(repo_path, "diff", range_, "--", rel) or "(no changes for this file)"


@mcp.tool()
def find_imports(repo: str, path: str, ref: str = "HEAD") -> str:
    """List the modules a JavaScript/TypeScript file imports, at a given ref.

    Call this to discover a changed file's direct dependencies so you can then
    `get_file` the ones relevant to your review. Recognises ES `import`,
    `export ... from`, dynamic `import()`, and CommonJS `require()`.

    Args:
        repo: Repository directory name.
        path: Repo-relative path to a .js/.jsx/.ts/.tsx file.
        ref: Commit SHA, branch, or tag. Defaults to "HEAD".
    """
    content = get_file(repo, path, ref)
    specifiers: list[str] = []
    seen: set[str] = set()
    patterns = (
        r"""import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""",
        r"""export\s+[^'"]*?\s+from\s+['"]([^'"]+)['"]""",
        r"""require\(\s*['"]([^'"]+)['"]\s*\)""",
        r"""import\(\s*['"]([^'"]+)['"]\s*\)""",
    )
    for pat in patterns:
        for m in re.finditer(pat, content):
            spec = m.group(1)
            if spec not in seen:
                seen.add(spec)
                specifiers.append(spec)
    if not specifiers:
        return "(no imports found)"
    return "\n".join(specifiers)


@mcp.tool()
def get_git_history(repo: str, path: str, limit: int = 20, ref: str = "HEAD") -> str:
    """Return the recent commit history for a file (one line per commit).

    Call this to understand why a file looks the way it does -- recent churn,
    who last touched the lines you are reviewing, whether a pattern is
    intentional. Each line: short-SHA, ISO date, author, subject.

    Args:
        repo: Repository directory name.
        path: Repo-relative path to the file.
        limit: Maximum number of commits to return (1-100). Defaults to 20.
        ref: Ref to start the log from. Defaults to "HEAD".
    """
    repo_path = _resolve_repo(repo)
    rel = _safe_relpath(path)
    limit = max(1, min(int(limit), 100))
    out = _git(
        repo_path,
        "log",
        ref,
        f"-{limit}",
        "--date=short",
        "--format=%h %ad %an %s",
        "--",
        rel,
    )
    return out.strip() or "(no history for this file)"


@mcp.tool()
def list_directory(repo: str, path: str = "", ref: str = "HEAD") -> str:
    """List the files and subdirectories at a path within a repo, at a ref.

    Call this to orient yourself before fetching files -- to find where a
    component, helper, or test lives. Directories are suffixed with "/".

    Args:
        repo: Repository directory name.
        path: Repo-relative directory ("" = repository root).
        ref: Commit SHA, branch, or tag. Defaults to "HEAD".
    """
    repo_path = _resolve_repo(repo)
    rel = "" if not path.strip() else _safe_relpath(path).rstrip("/") + "/"
    _ensure_ref(repo_path, ref)
    out = _git(repo_path, "ls-tree", "--name-only", "-z", f"{ref}:{rel}")
    entries = [e for e in out.split("\0") if e]
    if not entries:
        return "(empty)"
    # Mark directories: re-query the tree with object type.
    typed = _git(repo_path, "ls-tree", "-z", f"{ref}:{rel}")
    dirs = {
        line.split("\t", 1)[1]
        for line in typed.split("\0")
        if line and len(line.split()) >= 2 and line.split()[1] == "tree"
    }
    return "\n".join(
        sorted((f"{e}/" if e in dirs else e) for e in entries)
    )


# --------------------------------------------------------------------------- #
# Auth middleware + entrypoint
# --------------------------------------------------------------------------- #


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests whose bearer token does not match MCP_AUTH_TOKEN."""

    async def dispatch(self, request: Request, call_next):
        if MCP_AUTH_TOKEN:
            header = request.headers.get("authorization", "")
            token = header[7:].strip() if header.lower().startswith("bearer ") else ""
            if token != MCP_AUTH_TOKEN:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def main() -> None:
    if not REPOS_ROOT.exists():
        raise SystemExit(f"REPOS_ROOT does not exist: {REPOS_ROOT}")
    if not MCP_AUTH_TOKEN:
        print("WARNING: MCP_AUTH_TOKEN is unset -- the server is unauthenticated.")
    print(f"Serving repos from {REPOS_ROOT}: {', '.join(_list_repos()) or '(none)'}")

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)
    uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port)


if __name__ == "__main__":
    main()
