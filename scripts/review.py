"""Run a Claude review of a merge request's changed files.

Invoked by the CI job (see ci/templates/mr-review.yml). It:

1. Computes the changed source files between the MR's base and head commits.
2. Sends each changed file's full content -- with changed lines marked -- to
   Claude via the Messages API, along with the review guidelines and house
   rules, and the MCP server connection so Claude can fetch dependencies on
   demand.
3. Writes the structured findings to ``findings.json`` for post-review.py.

The model is steered to *report everything* with a confidence + severity on
each finding; filtering/ranking is left to a downstream step (here, the human
reading the MR). This matches the recommended Opus 4.x code-review prompting.

Environment (CI provides most of these as predefined variables):
    ANTHROPIC_API_KEY            (read implicitly by the SDK)
    MCP_SERVER_URL               https URL of the MCP server, e.g.
                                 https://mcp.example.com/mcp   (required)
    MCP_AUTH_TOKEN               bearer token for the MCP server (optional)
    CI_PROJECT_NAME              repo dir name as known to the MCP server
    CI_MERGE_REQUEST_DIFF_BASE_SHA   base commit of the MR diff
    CI_COMMIT_SHA                head commit
    REVIEW_MODEL                 override model (default claude-opus-4-8)
    REVIEW_OUTPUT                output path (default findings.json)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import anthropic

MODEL = os.environ.get("REVIEW_MODEL", "claude-opus-4-8")
OUTPUT = os.environ.get("REVIEW_OUTPUT", "findings.json")
MCP_BETA = "mcp-client-2025-11-20"
SOURCE_EXTENSIONS = {
    ".js", ".jsx", ".ts", ".tsx", ".vue", ".mjs", ".cjs", ".svelte",
}
MAX_FILE_BYTES = 100_000        # skip very large files; let Claude fetch on demand
MAX_CONTINUATIONS = 8           # cap pause_turn resumes
REPO_ROOT = Path(__file__).resolve().parent.parent

CHANGED_START = "<<< CHANGED START >>>"
CHANGED_END = "<<< CHANGED END >>>"


def _run(*args: str) -> str:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"command failed: {' '.join(args)}\n{proc.stderr}")
    return proc.stdout


def changed_files(base: str, head: str) -> list[str]:
    """Source files added or modified between base and head (deletions excluded)."""
    out = _run("git", "diff", "--name-only", "--diff-filter=d", f"{base}..{head}")
    files = []
    for name in out.splitlines():
        if Path(name).suffix in SOURCE_EXTENSIONS and Path(name).is_file():
            files.append(name)
    return files


def changed_line_set(base: str, head: str, path: str) -> set[int]:
    """New-file line numbers that were added or modified for ``path``."""
    out = _run("git", "diff", "-U0", f"{base}..{head}", "--", path)
    changed: set[int] = set()
    for line in out.splitlines():
        m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            for n in range(start, start + count):
                changed.add(n)
    return changed


def mark_file(path: str, changed: set[int]) -> str:
    """Render a file with line numbers and CHANGED markers around edited runs."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    rendered: list[str] = []
    inside = False
    for i, content in enumerate(lines, start=1):
        is_changed = i in changed
        if is_changed and not inside:
            rendered.append(CHANGED_START)
            inside = True
        elif not is_changed and inside:
            rendered.append(CHANGED_END)
            inside = False
        rendered.append(f"{i}: {content}")
    if inside:
        rendered.append(CHANGED_END)
    return "\n".join(rendered)


def load_guidelines() -> str:
    """Concatenate the review system prompt and any house rules."""
    parts: list[str] = []
    sys_prompt = REPO_ROOT / "prompts" / "review-system-prompt.md"
    if sys_prompt.exists():
        parts.append(sys_prompt.read_text(encoding="utf-8"))
    rules_dir = REPO_ROOT / "rules"
    if rules_dir.exists():
        for rule in sorted(rules_dir.glob("*.md")):
            parts.append(f"\n\n# House rule: {rule.stem}\n\n" + rule.read_text(encoding="utf-8"))
    return "\n".join(parts)


def build_user_message(repo: str, head: str, files: list[tuple[str, str]]) -> str:
    """Assemble the user turn: review instructions + the marked changed files."""
    header = (
        f"Review the changed files in merge request for repository `{repo}` "
        f"(head commit `{head}`).\n\n"
        f"The MCP server `ai-platform` is connected. Use its tools to fetch "
        f"context you need -- pass repo=\"{repo}\" and ref=\"{head}\":\n"
        f"  - get_file / list_directory  -> read dependencies and siblings\n"
        f"  - get_git_diff               -> see exactly what changed in a file\n"
        f"  - find_imports               -> discover a file's dependencies\n"
        f"  - get_git_history            -> understand why code looks as it does\n\n"
        f"Also read this repository's own `CLAUDE.md` (via get_file) if present, "
        f"for project-specific conventions.\n\n"
        f"Lines between {CHANGED_START} and {CHANGED_END} are the lines this MR "
        f"changed; concentrate your review there, but use surrounding context "
        f"and fetched dependencies to judge correctness.\n\n"
        f"Respond with ONLY valid JSON, no prose and no markdown fences, in the "
        f"shape documented in the system prompt.\n\n"
        f"=== CHANGED FILES ===\n"
    )
    blocks = [header]
    for path, marked in files:
        blocks.append(f"\n----- FILE: {path} -----\n{marked}\n")
    return "".join(blocks)


def extract_findings(text: str) -> list[dict]:
    """Parse the model's JSON output into a list of finding dicts."""
    text = text.strip()
    # Strip accidental code fences.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the first JSON object/array in the text.
        m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if not m:
            sys.exit(f"could not parse model output as JSON:\n{text[:2000]}")
        data = json.loads(m.group(1))
    findings = data.get("findings", data) if isinstance(data, dict) else data
    if not isinstance(findings, list):
        sys.exit(f"unexpected findings shape: {type(findings)}")
    return findings


def call_claude(system: str, user: str, repo: str) -> str:
    client = anthropic.Anthropic()
    mcp_url = os.environ["MCP_SERVER_URL"]
    server: dict = {"type": "url", "url": mcp_url, "name": "ai-platform"}
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if token:
        server["authorization_token"] = token

    messages: list[dict] = [{"role": "user", "content": user}]
    last_text = ""
    for _ in range(MAX_CONTINUATIONS + 1):
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=system,
            messages=messages,
            mcp_servers=[server],
            tools=[{"type": "mcp_toolset", "mcp_server_name": "ai-platform"}],
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            betas=[MCP_BETA],
        )
        last_text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
        if resp.stop_reason == "pause_turn":
            # Server-side tool loop hit its limit; resume.
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    return last_text


def main() -> None:
    repo = os.environ.get("CI_PROJECT_NAME", REPO_ROOT.name)
    base = os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or os.environ.get("BASE_SHA")
    head = os.environ.get("CI_COMMIT_SHA", "HEAD")
    if not base:
        sys.exit("base SHA unknown: set CI_MERGE_REQUEST_DIFF_BASE_SHA or BASE_SHA")

    names = changed_files(base, head)
    if not names:
        print("No changed source files; writing empty findings.")
        Path(OUTPUT).write_text("[]", encoding="utf-8")
        return

    marked_files: list[tuple[str, str]] = []
    for name in names:
        if Path(name).stat().st_size > MAX_FILE_BYTES:
            print(f"skip (too large): {name}")
            continue
        marked = mark_file(name, changed_line_set(base, head, name))
        marked_files.append((name, marked))

    print(f"Reviewing {len(marked_files)} file(s) in {repo} @ {head[:8]}")
    system = load_guidelines()
    user = build_user_message(repo, head, marked_files)
    text = call_claude(system, user, repo)
    findings = extract_findings(text)

    Path(OUTPUT).write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(f"Wrote {len(findings)} finding(s) to {OUTPUT}")


if __name__ == "__main__":
    main()
