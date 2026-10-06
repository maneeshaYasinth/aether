"""Per-container usage history from Prometheus (p95 and max over a time window).

metrics-server only knows "right now". Prometheus scrapes the same kubelet
(cAdvisor) numbers every minute and keeps them, so it can answer "what does
this container usually use, and what was its worst moment?" That is what a
request (typical load) and a limit (peak) should actually be sized from.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

# Skip the pod-level cgroup (container="") and the pause container ("POD"),
# which cAdvisor reports alongside the real containers.
SELECTOR = '{container!="",container!="POD"}'
BY = "sum by (namespace, pod, container)"

# Every query is evaluated at 5-minute steps across the window (a "subquery",
# the [window:5m] part), then reduced to one number per container.
CPU_RATE = f"{BY} (rate(container_cpu_usage_seconds_total{SELECTOR}[5m]))"
MEMORY = f"{BY} (container_memory_working_set_bytes{SELECTOR})"
QUERIES = {
    "cpu_p95": "quantile_over_time(0.95, ({cpu})[{window}:5m])",
    "cpu_max": "max_over_time(({cpu})[{window}:5m])",
    "memory_p95": "quantile_over_time(0.95, ({memory})[{window}:5m])",
    "memory_max": "max_over_time(({memory})[{window}:5m])",
    # How many 5-minute steps had data: times 5 minutes = hours of history.
    # Gaps (laptop asleep, k3s stopped) don't count, which is the point.
    "steps": "count_over_time(({memory})[{window}:5m])",
}


def query(prometheus_url, promql):
    """Run an instant query; return [(labels, value)]."""
    url = f"{prometheus_url.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.loads(response.read())
    if data.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {data.get('error')}")
    return [(r["metric"], float(r["value"][1])) for r in data["data"]["result"]]


def load_history(prometheus_url, window="7d"):
    """Return {(namespace, pod, container): {cpu_p95, cpu_max, memory_p95, memory_max, hours}}.

    CPU is in millicores and memory in MiB, matching the collector.
    """
    history = {}
    for name, template in QUERIES.items():
        promql = template.format(cpu=CPU_RATE, memory=MEMORY, window=window)
        for labels, value in query(prometheus_url, promql):
            key = (labels["namespace"], labels["pod"], labels["container"])
            history.setdefault(key, {})[name] = value

    for stats in history.values():
        for field in ("cpu_p95", "cpu_max"):
            if field in stats:
                stats[field] = round(stats[field] * 1000, 1)
        for field in ("memory_p95", "memory_max"):
            if field in stats:
                stats[field] = round(stats[field] / (1024 * 1024), 1)
        stats["hours"] = round(stats.pop("steps", 0) * 5 / 60, 1)
    return history


def owning_workload(pod, workload_names):
    """Guess which workload a (possibly long-gone) pod belonged to from its name.

    Old pods no longer exist to look up, but their names keep the workload as a
    prefix: hello-nginx-847949887d-x2x7p, argocd-application-controller-0.
    The longest match wins, so "foo-bar-..." isn't credited to "foo".
    """
    matches = [w for w in workload_names if pod == w or pod.startswith(w + "-")]
    return max(matches, key=len) if matches else None


def by_workload(history, workloads):
    """Fold per-pod history into {(namespace, workload, container): stats}.

    workloads is {namespace: {workload names}} for what runs now. Every pod a
    workload has had (each rollout makes new ones) contributes, and the
    highest p95/max wins, the safe direction for sizing. Hours take the
    longest single pod rather than a sum, because replicas overlap in time;
    after a rollout this undercounts, which only means falling back to the
    snapshot sooner.
    """
    grouped = {}
    for (namespace, pod, container), stats in history.items():
        workload = owning_workload(pod, workloads.get(namespace, ()))
        if not workload:
            continue
        merged = grouped.setdefault((namespace, workload, container), {})
        for field, value in stats.items():
            merged[field] = max(merged.get(field, value), value)
    return grouped
