"""Post Claude's review findings as inline comments on a GitLab merge request.

Reads ``findings.json`` (produced by review.py) and creates one GitLab
discussion per finding, anchored to the exact file and line via diff position
SHAs. Findings that cannot be anchored to the diff (e.g. a line GitLab does not
consider part of the change) fall back to an unanchored MR note so nothing is
silently dropped.

Uses only the Python standard library -- no pip dependencies -- so the CI job
needs nothing beyond Python itself.

Finding shape (each item in findings.json):
    {
      "file":       "src/components/Foo.tsx",   # repo-relative path (required)
      "line":       42,                          # new-file line number (required)
      "severity":   "blocker|major|minor|nit",   # (optional, default "minor")
      "confidence": "high|medium|low",           # (optional)
      "category":   "correctness|...",           # (optional)
      "comment":    "..."                         # the review note (required)
    }

Environment (GitLab CI predefined variables, plus a token):
    GITLAB_TOKEN             token with `api` scope (personal/project/group)
    CI_API_V4_URL            e.g. https://gitlab.example.com/api/v4
    CI_PROJECT_ID            numeric project id
    CI_MERGE_REQUEST_IID     MR internal id

Usage:
    python post-review.py [findings.json]
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SEVERITY_LABEL = {
    "blocker": "\U0001F534 Blocker",   # red circle
    "major": "\U0001F7E0 Major",       # orange circle
    "minor": "\U0001F7E1 Minor",       # yellow circle
    "nit": "\U0001F4DD Nit",           # memo
}


def _request(method: str, url: str, token: str, fields: dict | None = None) -> dict:
    data = urllib.parse.urlencode(fields, doseq=True).encode() if fields else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("PRIVATE-TOKEN", token)
    if data:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def get_diff_refs(api: str, project: str, mr_iid: str, token: str) -> dict:
    url = f"{api}/projects/{urllib.parse.quote(project, safe='')}/merge_requests/{mr_iid}"
    mr = _request("GET", url, token)
    refs = mr.get("diff_refs")
    if not refs:
        sys.exit("MR has no diff_refs; cannot anchor inline comments")
    return refs


def format_body(finding: dict) -> str:
    sev = (finding.get("severity") or "minor").lower()
    label = SEVERITY_LABEL.get(sev, SEVERITY_LABEL["minor"])
    parts = [f"**{label}**"]
    if finding.get("confidence"):
        parts.append(f"· confidence: {finding['confidence']}")
    if finding.get("category"):
        parts.append(f"· {finding['category']}")
    header = " ".join(parts)
    return f"{header}\n\n{finding.get('comment', '').strip()}\n\n_— Claude review_"


def post_inline(api, project, mr_iid, token, refs, finding) -> bool:
    url = (
        f"{api}/projects/{urllib.parse.quote(project, safe='')}"
        f"/merge_requests/{mr_iid}/discussions"
    )
    fields = {
        "body": format_body(finding),
        "position[position_type]": "text",
        "position[base_sha]": refs["base_sha"],
        "position[start_sha]": refs["start_sha"],
        "position[head_sha]": refs["head_sha"],
        "position[new_path]": finding["file"],
        "position[old_path]": finding["file"],
        "position[new_line]": str(finding["line"]),
    }
    try:
        _request("POST", url, token, fields)
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"  inline post failed ({e.code}) for {finding['file']}:"
              f"{finding.get('line')} -> {detail[:200]}")
        return False


def post_note(api, project, mr_iid, token, finding) -> None:
    """Unanchored fallback so a finding is never silently lost."""
    url = (
        f"{api}/projects/{urllib.parse.quote(project, safe='')}"
        f"/merge_requests/{mr_iid}/notes"
    )
    loc = f"`{finding.get('file')}:{finding.get('line')}` — "
    body = loc + format_body(finding)
    try:
        _request("POST", url, token, {"body": body})
    except urllib.error.HTTPError as e:
        print(f"  note fallback failed ({e.code}): {e.read().decode(errors='replace')[:200]}")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "findings.json"
    token = os.environ.get("GITLAB_TOKEN")
    api = os.environ.get("CI_API_V4_URL")
    project = os.environ.get("CI_PROJECT_ID")
    mr_iid = os.environ.get("CI_MERGE_REQUEST_IID")
    missing = [k for k, v in {
        "GITLAB_TOKEN": token, "CI_API_V4_URL": api,
        "CI_PROJECT_ID": project, "CI_MERGE_REQUEST_IID": mr_iid,
    }.items() if not v]
    if missing:
        sys.exit(f"missing required environment: {', '.join(missing)}")

    findings = json.loads(open(path, encoding="utf-8").read())
    if not findings:
        print("No findings to post.")
        return

    refs = get_diff_refs(api, project, mr_iid, token)
    posted = fell_back = 0
    for finding in findings:
        if not finding.get("file") or finding.get("line") is None:
            post_note(api, project, mr_iid, token, finding)
            fell_back += 1
            continue
        if post_inline(api, project, mr_iid, token, refs, finding):
            posted += 1
        else:
            post_note(api, project, mr_iid, token, finding)
            fell_back += 1

    print(f"Posted {posted} inline comment(s); {fell_back} fell back to MR notes.")


if __name__ == "__main__":
    main()
