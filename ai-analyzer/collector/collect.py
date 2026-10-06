"""Collect per-container CPU/memory usage alongside requests and limits.

Reads live usage from metrics-server (the metrics.k8s.io API) and the declared
requests/limits from each pod's spec, joins them per container, and prints
JSON for the analyzer to consume. With a Prometheus URL it also adds p95/max
usage over a time window, per workload, and the analyzer sizes from those
instead of the snapshot once there's enough history.

Usage:
    python collect.py                       # all namespaces
    python collect.py -n hello-nginx        # one namespace
    python collect.py --exclude kube-system --exclude argocd
    python collect.py --prometheus http://localhost:9090   # after a port-forward
"""

import argparse
import json
import os
import sys
import time
import urllib.error
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.utils import parse_quantity

from history import by_workload, load_history

# Less than a day can't include a daily peak, so it's no better than a snapshot.
MIN_HISTORY_HOURS = 24


def to_millicores(quantity):
    """'250m' -> 250, '1' -> 1000, '12345n' -> 0.012 (rounded to 1 decimal)."""
    if quantity is None:
        return None
    return round(float(parse_quantity(quantity) * 1000), 1)


def to_mebibytes(quantity):
    """'128Mi' -> 128, '9216Ki' -> 9.0, '1Gi' -> 1024."""
    if quantity is None:
        return None
    return round(float(parse_quantity(quantity) / (1024 * 1024)), 1)


def percent(used, reference):
    """How much of a request/limit is actually used. None if nothing was declared."""
    if used is None or not reference:
        return None
    return round(used / reference * 100, 1)


def load_usage(custom_api, namespace, retries=5):
    """Return {(namespace, pod, container): {"cpu": ..., "memory": ...}} from metrics-server.

    Retries on 503: if the cluster was off, the CronJob's missed run fires the
    moment k3s starts, before metrics-server is ready (5+10+20+40+60s, ~2 min).
    """
    for attempt in range(retries + 1):
        try:
            if namespace:
                result = custom_api.list_namespaced_custom_object(
                    "metrics.k8s.io", "v1beta1", namespace, "pods"
                )
            else:
                result = custom_api.list_cluster_custom_object(
                    "metrics.k8s.io", "v1beta1", "pods"
                )
            break
        except ApiException as error:
            if error.status != 503 or attempt == retries:
                raise
            delay = min(5 * 2 ** attempt, 60)
            print(f"Metrics API not ready (503), retrying in {delay}s...", file=sys.stderr)
            time.sleep(delay)

    usage = {}
    for pod in result["items"]:
        ns = pod["metadata"]["namespace"]
        name = pod["metadata"]["name"]
        for container in pod["containers"]:
            usage[(ns, name, container["name"])] = container["usage"]
    return usage


def resolve_workload(apps_api, pod, cache):
    """Return the object a human actually edits in git, e.g. ("Deployment", "hello-nginx").

    A Deployment's pods are owned by a ReplicaSet (hello-nginx-847949887d), which
    is generated and never edited directly, so follow that one extra hop.
    """
    owners = pod.metadata.owner_references or []
    if not owners:
        return {"kind": "Pod", "name": pod.metadata.name}

    owner = owners[0]
    if owner.kind != "ReplicaSet":
        # StatefulSet, DaemonSet, Job... already the thing you edit.
        return {"kind": owner.kind, "name": owner.name}

    key = (pod.metadata.namespace, owner.name)
    if key not in cache:
        rs = apps_api.read_namespaced_replica_set(owner.name, pod.metadata.namespace)
        rs_owners = rs.metadata.owner_references or []
        if rs_owners:
            cache[key] = {"kind": rs_owners[0].kind, "name": rs_owners[0].name}
        else:
            cache[key] = {"kind": "ReplicaSet", "name": owner.name}
    return cache[key]


def collect(namespace=None, exclude=(), prometheus_url=None, window="7d"):
    # Uses ~/.kube/config locally; falls back to the pod's service account
    # if this ever runs inside the cluster.
    try:
        config.load_kube_config()
    except config.ConfigException:
        config.load_incluster_config()

    core_api = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()
    apps_api = client.AppsV1Api()
    workload_cache = {}

    usage = load_usage(custom_api, namespace)
    if namespace:
        pods = core_api.list_namespaced_pod(namespace).items
    else:
        pods = core_api.list_pod_for_all_namespaces().items

    # Resolve workloads first: history is grouped by workload, and that needs
    # to know every workload name in a namespace.
    running = [
        (pod, resolve_workload(apps_api, pod, workload_cache))
        for pod in pods
        if pod.metadata.namespace not in exclude and pod.status.phase == "Running"
    ]
    history, source = {}, "metrics-server (point-in-time snapshot)"
    if prometheus_url:
        try:
            workloads = {}
            for pod, workload in running:
                workloads.setdefault(pod.metadata.namespace, set()).add(workload["name"])
            history = by_workload(load_history(prometheus_url, window), workloads)
            source = f"Prometheus p95/max over {window} where >= {MIN_HISTORY_HOURS}h of history, else metrics-server snapshot"
        except (urllib.error.URLError, OSError, RuntimeError, ValueError) as error:
            # History is an improvement, not a requirement: never fail the run over it.
            print(f"Prometheus unavailable ({error}); using the metrics-server snapshot only", file=sys.stderr)

    containers = []
    for pod, workload in running:
        ns = pod.metadata.namespace
        for spec in pod.spec.containers:
            resources = spec.resources
            requests = (resources.requests or {}) if resources else {}
            limits = (resources.limits or {}) if resources else {}
            used = usage.get((ns, pod.metadata.name, spec.name), {})
            past = history.get((ns, workload["name"], spec.name), {})
            hours = past.get("hours", 0)
            has_history = hours >= MIN_HISTORY_HOURS

            cpu = {"used": to_millicores(used.get("cpu"))}
            memory = {"used": to_mebibytes(used.get("memory"))}
            if has_history:
                cpu.update(p95=past.get("cpu_p95"), max=past.get("cpu_max"))
                memory.update(p95=past.get("memory_p95"), max=past.get("memory_max"))
            cpu.update(
                request=to_millicores(requests.get("cpu")),
                limit=to_millicores(limits.get("cpu")),
            )
            memory.update(
                request=to_mebibytes(requests.get("memory")),
                limit=to_mebibytes(limits.get("memory")),
            )
            # Requests are judged against typical load (p95), limits against the
            # worst moment (max). Without history both use the snapshot.
            for stats in (cpu, memory):
                typical = stats.get("p95", stats["used"])
                peak = stats.get("max", stats["used"])
                stats["pct_of_request"] = percent(typical, stats["request"])
                stats["pct_of_limit"] = percent(peak, stats["limit"])

            containers.append({
                "namespace": ns,
                "pod": pod.metadata.name,
                "container": spec.name,
                "workload": workload,
                "cpu_millicores": cpu,
                "memory_mebibytes": memory,
                "usage_basis": "history" if has_history else "snapshot",
                "history_hours": hours,
                "metrics_available": bool(used) or has_history,
            })

    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "containers": containers,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", "--namespace", help="only collect from this namespace")
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="NAMESPACE",
        help="skip a namespace (repeatable)",
    )
    parser.add_argument(
        "--prometheus", default=os.environ.get("PROMETHEUS_URL"),
        help="Prometheus base URL for usage history (default: $PROMETHEUS_URL; none = snapshot only)",
    )
    parser.add_argument("--window", default="7d", help="how far back to look in Prometheus (default: 7d)")
    parser.add_argument("-o", "--output", help="write JSON to this file instead of stdout")
    args = parser.parse_args()

    snapshot = collect(args.namespace, set(args.exclude), args.prometheus, args.window)
    text = json.dumps(snapshot, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(text + "\n")
        print(f"Wrote {len(snapshot['containers'])} containers to {args.output}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
