"""Turn a collector snapshot into rightsizing recommendations using Gemini.

The collector does all the arithmetic (units, percentages); Gemini only does
the reasoning. Its reply is forced into a JSON schema so the output can be
validated and rendered, not just read.

Usage:
    python analyzer/analyze.py                         # collect live, then analyze
    python analyzer/analyze.py -i snapshot.json        # analyze a saved snapshot
    python analyzer/analyze.py --dry-run               # print the prompt, don't call Gemini
    python analyzer/analyze.py -o recs.json --markdown recs.md
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
from collect import collect  # noqa: E402

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.8-flash"

# Where each workload's resources are actually declared, so a recommendation
# points at a file to edit rather than at a generated pod. Checked by
# (namespace, workload name) first, then by namespace.
CONFIG_SOURCES = {
    ("hello-nginx", "hello-nginx"): "gitops/charts/hello-nginx/deployment.yaml",
    ("kube-system", "metrics-server"): "gitops/apps/metrics-server.yaml (Helm values)",
    ("argocd", None): "terraform/modules/argocd-bootstrap/main.tf (Helm set values)",
    ("kube-system", None): "managed by k3s itself, not by this repo",
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "recommendations": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "namespace": {"type": "STRING"},
                    "workload": {"type": "STRING"},
                    "container": {"type": "STRING"},
                    "issue": {
                        "type": "STRING",
                        "enum": ["over_provisioned", "under_provisioned", "missing_requests", "missing_limits", "ok"],
                    },
                    "severity": {"type": "STRING", "enum": ["high", "medium", "low"]},
                    "current": {
                        "type": "OBJECT",
                        "properties": {
                            "cpu_request": {"type": "STRING"},
                            "cpu_limit": {"type": "STRING"},
                            "memory_request": {"type": "STRING"},
                            "memory_limit": {"type": "STRING"},
                        },
                    },
                    "recommended": {
                        "type": "OBJECT",
                        "properties": {
                            "cpu_request": {"type": "STRING"},
                            "cpu_limit": {"type": "STRING"},
                            "memory_request": {"type": "STRING"},
                            "memory_limit": {"type": "STRING"},
                        },
                    },
                    "reasoning": {"type": "STRING"},
                    "where_to_change": {"type": "STRING"},
                    "confidence": {"type": "STRING", "enum": ["high", "medium", "low"]},
                },
                "required": ["namespace", "workload", "container", "issue", "severity", "reasoning", "confidence"],
            },
        },
    },
    "required": ["summary", "recommendations"],
}


def config_source(namespace, workload_name):
    return (
        CONFIG_SOURCES.get((namespace, workload_name))
        or CONFIG_SOURCES.get((namespace, None))
        or "unknown"
    )


def build_prompt(snapshot):
    containers = []
    for c in snapshot["containers"]:
        workload = c["workload"]
        containers.append({
            "namespace": c["namespace"],
            "workload": f"{workload['kind']}/{workload['name']}",
            "container": c["container"],
            "cpu_millicores": c["cpu_millicores"],
            "memory_mebibytes": c["memory_mebibytes"],
            "metrics_available": c["metrics_available"],
            "config_source": config_source(c["namespace"], workload["name"]),
        })

    return f"""You are a Kubernetes capacity-planning assistant reviewing a small cluster
for a junior DevOps engineer's learning project. Recommend CPU/memory request and
limit changes based on the usage data below.

Data notes:
- CPU is in millicores (1000 = 1 core), memory in MiB. All arithmetic is already done;
  use the numbers given rather than recalculating them.
- null request/limit means none is set. null usage means metrics-server had no data.
- This is ONE point-in-time snapshot from metrics-server ({snapshot["collected_at"]}),
  not a history. Peaks are invisible, so never recommend cutting below a safe floor,
  and lower your confidence accordingly.
- config_source says where the values are declared. If it says "managed by k3s",
  recommend leaving it alone unless there is a real problem.

Rules:
- Over-provisioned: usage under ~20% of request. Recommend a request of roughly
  2x observed usage, with a floor of 10m CPU and 32Mi memory.
- Under-provisioned: usage over ~80% of request or near the limit.
- Missing requests: the scheduler cannot place the pod sensibly and it is first to be
  evicted under pressure. Suggest starting values from observed usage.
- Memory limits should be at least 1.5x the recommended memory request; going over a
  memory limit kills the container, going over a CPU limit only throttles it.
- Write quantities in Kubernetes notation (e.g. "50m", "64Mi").
- Include one entry per container, using issue "ok" when nothing needs to change.
- Keep reasoning to 1-2 plain-English sentences that explain the why.

Snapshot:
{json.dumps(containers, indent=2)}
"""


def retry_delay(error, attempt):
    """Exponential backoff (2s, 4s, 8s, 16s), honouring Retry-After if Google sends one."""
    retry_after = error.headers.get("Retry-After") if error.headers else None
    try:
        return min(max(float(retry_after), 2 ** (attempt + 1)), 60)
    except (TypeError, ValueError):
        return min(2 ** (attempt + 1), 60)


def quota_exhausted(details):
    """True when a 429 is a spent quota (daily cap, or a model with zero free-tier
    quota) rather than a per-minute rate limit. Waiting won't fix the former, so
    the caller should move straight to the next model.
    """
    try:
        error = json.loads(details).get("error", {})
    except (ValueError, AttributeError):
        return False
    for detail in error.get("details", []):
        for violation in detail.get("violations", []):
            if "PerDay" in violation.get("quotaId", "") or str(violation.get("quotaValue")) == "0":
                return True
    return False


def fallback_models(api_key, preferred, limit=3):
    """Other Flash models this key can call, newest-looking first.

    Same idea as GuardRail: if one model is overloaded or retired, ask the API
    what is available instead of hard-coding a list that goes stale.
    """
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        headers={"x-goog-api-key": api_key},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
    except urllib.error.URLError:
        return []

    names = []
    for model in data.get("models", []):
        name = model.get("name", "").removeprefix("models/")
        if (
            "generateContent" in model.get("supportedGenerationMethods", [])
            and "flash" in name
            and not any(skip in name for skip in ("embedding", "image", "tts", "live", "audio"))
            and name != preferred
        ):
            names.append(name)
    return sorted(names, reverse=True)[:limit]


def call_gemini(prompt, max_retries=5):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY is not set. Run: export GEMINI_API_KEY=...")
    preferred = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0.2,
        },
    }).encode("utf-8")

    models = [preferred]
    errors = []
    while models:
        model = models.pop(0)
        request = urllib.request.Request(
            GEMINI_URL.format(model=model),
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        )
        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    data = json.loads(response.read())
                if model != preferred:
                    print(f"Note: answered by fallback model {model}", file=sys.stderr)
                return json.loads(data["candidates"][0]["content"]["parts"][0]["text"])
            except urllib.error.HTTPError as error:
                details = error.read().decode("utf-8", errors="replace")
                if error.code == 429 and quota_exhausted(details):
                    print(f"{model} quota exhausted, trying next model", file=sys.stderr)
                elif error.code in (429, 503) and attempt < max_retries - 1:
                    delay = retry_delay(error, attempt)
                    print(f"{model} busy ({error.code}), retrying in {delay:.0f}s...", file=sys.stderr)
                    time.sleep(delay)
                    continue
                if error.code not in (404, 429, 503):
                    # 400/401/403 mean our request or key is wrong; another model won't help.
                    raise RuntimeError(f"Gemini returned {error.code} for model {model}: {details}") from error
                errors.append(f"{model}: {error.code}")
                break

        # Only look up fallbacks once, after the preferred model has failed.
        if model == preferred:
            models = fallback_models(api_key, preferred)
            if models:
                print(f"Falling back to: {', '.join(models)}", file=sys.stderr)

    raise RuntimeError(f"All Gemini models failed ({'; '.join(errors)}). Try again in a few minutes.")


def parse_quantity(value):
    """Turn "50m", "0.05", "64Mi", "1Gi" into a number so equal values written
    differently compare equal. Returns None if it doesn't look like a quantity.
    """
    units = {"m": 0.001, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "k": 1e3, "M": 1e6, "G": 1e9}
    text = str(value).strip()
    for suffix in sorted(units, key=len, reverse=True):
        if text.endswith(suffix):
            text, factor = text[: -len(suffix)], units[suffix]
            break
    else:
        factor = 1
    try:
        return float(text) * factor
    except ValueError:
        return None


def mark_noops(result):
    """Models sometimes flag a container but "recommend" exactly its current values.
    Relabel those as "ok" so they drop out of the report and the PR.
    """
    for rec in result["recommendations"]:
        if rec["issue"] == "ok":
            continue
        current, recommended = rec.get("current") or {}, rec.get("recommended") or {}
        proposed = {k: v for k, v in recommended.items() if quantity(v) != "—"}
        if proposed and all(
            parse_quantity(v) is not None and parse_quantity(v) == parse_quantity(current.get(k))
            for k, v in proposed.items()
        ):
            rec["issue"] = "ok"
    return result


def quantity(value):
    """Models sometimes write the word "null" instead of omitting a field."""
    if not value or str(value).strip().lower() in ("null", "none", "n/a"):
        return "—"
    return value


def render_markdown(result, snapshot):
    actionable = [r for r in result["recommendations"] if r["issue"] != "ok"]
    order = {"high": 0, "medium": 1, "low": 2}
    actionable.sort(key=lambda r: order.get(r["severity"], 3))

    lines = [
        "## Aether rightsizing report",
        "",
        result["summary"],
        "",
        f"_Snapshot: {snapshot['collected_at']} · {len(snapshot['containers'])} containers · "
        f"{len(actionable)} with recommendations_",
        "",
    ]
    if not actionable:
        lines.append("No changes recommended.")
        return "\n".join(lines) + "\n"

    lines += [
        "| Severity | Workload | Issue | CPU req | Mem req | Confidence |",
        "|---|---|---|---|---|---|",
    ]
    for r in actionable:
        cur, rec = r.get("current") or {}, r.get("recommended") or {}
        cpu = f"{quantity(cur.get('cpu_request'))} → {quantity(rec.get('cpu_request'))}"
        mem = f"{quantity(cur.get('memory_request'))} → {quantity(rec.get('memory_request'))}"
        lines.append(
            f"| {r['severity']} | `{r['namespace']}/{r['workload']}` ({r['container']}) | "
            f"{r['issue'].replace('_', ' ')} | {cpu} | {mem} | {r['confidence']} |"
        )

    lines += ["", "### Details", ""]
    for r in actionable:
        lines.append(f"- **`{r['namespace']}/{r['workload']}`**: {r['reasoning']}")
        if r.get("where_to_change"):
            lines.append(f"  - Change in: `{r['where_to_change']}`")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-i", "--input", help="collector snapshot JSON (default: collect live)")
    parser.add_argument("--exclude", action="append", default=[], metavar="NAMESPACE",
                        help="when collecting live, skip a namespace (repeatable)")
    parser.add_argument("-o", "--output", help="write recommendations JSON here")
    parser.add_argument("--markdown", help="write a Markdown report here (PR-comment ready)")
    parser.add_argument("--dry-run", action="store_true", help="print the prompt and exit")
    args = parser.parse_args()

    if args.input:
        with open(args.input) as f:
            snapshot = json.load(f)
    else:
        snapshot = collect(exclude=set(args.exclude))

    prompt = build_prompt(snapshot)
    if args.dry_run:
        print(prompt)
        return

    result = mark_noops(call_gemini(prompt))

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
    markdown = render_markdown(result, snapshot)
    if args.markdown:
        with open(args.markdown, "w") as f:
            f.write(markdown)
    print(markdown)


if __name__ == "__main__":
    main()
