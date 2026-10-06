"""Full Aether pipeline: collect metrics -> ask Gemini -> open a rightsizing PR.

This is what the in-cluster CronJob runs.

Usage:
    python run.py --dry-run                    # show the diff + PR body, change nothing
    python run.py --exclude kube-system        # open a real PR (needs GEMINI_API_KEY, GITHUB_TOKEN)
    python run.py -i recs.json --dry-run       # reuse saved recommendations, skip Gemini
    PROMETHEUS_URL=http://localhost:9090 python run.py --dry-run   # size from history
"""

import argparse
import json
import os
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
for folder in ("collector", "analyzer", "proposer"):
    sys.path.insert(0, str(here / folder))

from analyze import build_prompt, call_gemini, mark_noops, render_markdown  # noqa: E402
from collect import collect  # noqa: E402
from propose import propose  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exclude", action="append", default=[], metavar="NAMESPACE",
                        help="skip a namespace (repeatable)")
    parser.add_argument("--prometheus", default=os.environ.get("PROMETHEUS_URL"),
                        help="Prometheus URL for usage history (default: $PROMETHEUS_URL)")
    parser.add_argument("-i", "--recommendations", help="use saved recommendations JSON instead of calling Gemini")
    parser.add_argument("--dry-run", action="store_true", help="print the diff and PR body; don't touch GitHub")
    args = parser.parse_args()

    snapshot = collect(exclude=set(args.exclude), prometheus_url=args.prometheus)
    print(f"Collected {len(snapshot['containers'])} containers", file=sys.stderr)

    if args.recommendations:
        with open(args.recommendations) as f:
            result = json.load(f)
    else:
        result = call_gemini(build_prompt(snapshot))
    mark_noops(result)

    # Always print the full report, dry run or not: most findings aren't
    # auto-editable, and without this they'd only be visible if a PR happened
    # to be opened.
    report = render_markdown(result, snapshot)
    print(report)
    propose(result, report, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
