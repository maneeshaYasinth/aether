"""Turn analyzer recommendations into a GitHub pull request that edits the YAML.

The AI only *proposes*: every change lands as a PR a human must review and
merge, and only then does Argo CD apply it. Nothing here touches the cluster.

Each recommendation is validated before it is written, because model output
is untrusted input: quantities must parse, limits must be >= requests, and
only manifests listed in EDITABLE_MANIFESTS can ever be edited.
"""

import base64
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from kubernetes.utils import parse_quantity
from ruamel.yaml import YAML

API = "https://api.github.com"
BRANCH_PREFIX = "aether/rightsizing-"

# The only files the proposer is allowed to change, keyed by (namespace, "Kind/name").
# Everything else (Helm values, Terraform, k3s-managed pods) stays report-only.
EDITABLE_MANIFESTS = {
    ("hello-nginx", "Deployment/hello-nginx"): "gitops/charts/hello-nginx/deployment.yaml",
}

CPU_PATTERN = re.compile(r"^\d+(\.\d+)?m?$")
MEMORY_PATTERN = re.compile(r"^\d+(\.\d+)?(Ki|Mi|Gi)$")
RESOURCE_FIELDS = {
    "cpu_request": ("requests", "cpu", CPU_PATTERN),
    "cpu_limit": ("limits", "cpu", CPU_PATTERN),
    "memory_request": ("requests", "memory", MEMORY_PATTERN),
    "memory_limit": ("limits", "memory", MEMORY_PATTERN),
}


class GitHub:
    def __init__(self, repo, token=None):
        self.repo = repo
        self.token = token

    def request(self, method, path, body=None):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{API}/repos/{self.repo}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.loads(response.read() or "null")
        except urllib.error.HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub {method} {path} returned {error.code}: {details}") from error

    def get_file(self, path, ref):
        data = self.request("GET", f"/contents/{path}?ref={ref}")
        return base64.b64decode(data["content"]).decode(), data["sha"]


def validate(recommended):
    """Return (clean values, problems). Drops anything that isn't a sane Kubernetes quantity."""
    clean, problems = {}, []
    for field, (_, _, pattern) in RESOURCE_FIELDS.items():
        value = (recommended or {}).get(field)
        if not value or str(value).lower() in ("null", "none", "n/a"):
            continue
        if not pattern.match(str(value)):
            problems.append(f"{field}={value!r} is not a valid quantity")
            continue
        clean[field] = str(value)

    for kind in ("cpu", "memory"):
        req, lim = clean.get(f"{kind}_request"), clean.get(f"{kind}_limit")
        if req and lim and parse_quantity(lim) < parse_quantity(req):
            problems.append(f"{kind} limit {lim} is below request {req}")
            clean.pop(f"{kind}_limit")
    return clean, problems


def apply_to_manifest(text, container_name, values):
    """Set resources on one container in a Deployment manifest, keeping comments and layout."""
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    doc = yaml.load(text)

    for container in doc["spec"]["template"]["spec"]["containers"]:
        if container["name"] != container_name:
            continue
        resources = container.setdefault("resources", {})
        for field, value in values.items():
            section, key, _ = RESOURCE_FIELDS[field]
            resources.setdefault(section, {})[key] = value
        out = io.StringIO()
        yaml.dump(doc, out)
        return out.getvalue()
    raise ValueError(f"container {container_name!r} not found")


def plan_changes(result, github, base="main"):
    """Work out which files to change. Returns (files, applied, skipped)."""
    files, applied, skipped = {}, [], []
    for rec in result["recommendations"]:
        if rec["issue"] == "ok":
            continue
        label = f"{rec['namespace']}/{rec['workload']}"
        path = EDITABLE_MANIFESTS.get((rec["namespace"], rec["workload"]))
        if not path:
            skipped.append((rec, "not in an auto-editable manifest; change manually"))
            continue

        values, problems = validate(rec.get("recommended"))
        if problems:
            print(f"{label}: {'; '.join(problems)}", file=sys.stderr)
        if not values:
            skipped.append((rec, "no valid values in recommendation"))
            continue

        if path not in files:
            files[path] = {"original": github.get_file(path, base)}
            files[path]["text"] = files[path]["original"][0]
        files[path]["text"] = apply_to_manifest(files[path]["text"], rec["container"], values)
        applied.append((rec, path, values))

    files = {p: f for p, f in files.items() if f["text"] != f["original"][0]}
    return files, applied, skipped


def pr_body(report_markdown, applied, skipped):
    lines = [report_markdown.strip(), "", "---", "", "### Changes in this PR", ""]
    for rec, path, values in applied:
        changes = ", ".join(f"{k.replace('_', ' ')} → `{v}`" for k, v in values.items())
        lines.append(f"- `{path}` ({rec['container']}): {changes}")
    if skipped:
        lines += ["", "### Not changed automatically", ""]
        for rec, reason in skipped:
            lines.append(f"- `{rec['namespace']}/{rec['workload']}`: {reason}")
    lines += [
        "",
        "> Generated by the Aether analyzer (data source in the report above). "
        "Review before merging; Argo CD applies the change once merged.",
    ]
    return "\n".join(lines)


def open_pull_request(github, files, body, base="main"):
    open_prs = github.request("GET", "/pulls?state=open&per_page=100")
    for pr in open_prs:
        if pr["head"]["ref"].startswith(BRANCH_PREFIX):
            print(f"Rightsizing PR already open, not creating another: {pr['html_url']}", file=sys.stderr)
            return pr["html_url"]

    branch = BRANCH_PREFIX + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    base_sha = github.request("GET", f"/git/ref/heads/{base}")["object"]["sha"]
    github.request("POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})

    for path, f in files.items():
        github.request("PUT", f"/contents/{path}", {
            "message": f"chore(rightsizing): update resources in {path}",
            "content": base64.b64encode(f["text"].encode()).decode(),
            "sha": f["original"][1],
            "branch": branch,
        })

    pr = github.request("POST", "/pulls", {
        "title": "Aether: rightsizing recommendations",
        "head": branch,
        "base": base,
        "body": body,
    })
    return pr["html_url"]


def propose(result, report_markdown, dry_run=False):
    repo = os.environ.get("GITHUB_REPO", "maneeshaYasinth/aether")
    token = os.environ.get("GITHUB_TOKEN")
    if not dry_run and not token:
        sys.exit("GITHUB_TOKEN is not set (needed to open a PR). Use --dry-run to preview.")

    github = GitHub(repo, token)
    files, applied, skipped = plan_changes(result, github)
    body = pr_body(report_markdown, applied, skipped)

    if not files:
        print("No auto-editable changes; no PR opened.", file=sys.stderr)
        return None

    if dry_run:
        import difflib
        for path, f in files.items():
            diff = difflib.unified_diff(
                f["original"][0].splitlines(keepends=True), f["text"].splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}",
            )
            sys.stdout.writelines(diff)
        print("\n--- PR body ---\n" + body)
        return None

    url = open_pull_request(github, files, body)
    print(f"Pull request: {url}", file=sys.stderr)
    return url
