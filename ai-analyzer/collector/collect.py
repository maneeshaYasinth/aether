"""Collect per-container CPU/memory usage alongside requests and limits.

Reads live usage from metrics-server (the metrics.k8s.io API) and the declared
requests/limits from each pod's spec, joins them per container, and prints
JSON for the analyzer to consume.

Usage:
    python collect.py                       # all namespaces
    python collect.py -n hello-nginx        # one namespace
    python collect.py --exclude kube-system --exclude argocd
"""

import argparse
import json
import sys
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.utils import parse_quantity


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


def load_usage(custom_api, namespace):
    """Return {(namespace, pod, container): {"cpu": ..., "memory": ...}} from metrics-server."""
    if namespace:
        result = custom_api.list_namespaced_custom_object(
            "metrics.k8s.io", "v1beta1", namespace, "pods"
        )
    else:
        result = custom_api.list_cluster_custom_object(
            "metrics.k8s.io", "v1beta1", "pods"
        )

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


def collect(namespace=None, exclude=()):
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

    containers = []
    for pod in pods:
        ns = pod.metadata.namespace
        if ns in exclude or pod.status.phase != "Running":
            continue

        workload = resolve_workload(apps_api, pod, workload_cache)

        for spec in pod.spec.containers:
            resources = spec.resources
            requests = (resources.requests or {}) if resources else {}
            limits = (resources.limits or {}) if resources else {}
            used = usage.get((ns, pod.metadata.name, spec.name), {})

            cpu_used = to_millicores(used.get("cpu"))
            cpu_request = to_millicores(requests.get("cpu"))
            cpu_limit = to_millicores(limits.get("cpu"))
            mem_used = to_mebibytes(used.get("memory"))
            mem_request = to_mebibytes(requests.get("memory"))
            mem_limit = to_mebibytes(limits.get("memory"))

            containers.append({
                "namespace": ns,
                "pod": pod.metadata.name,
                "container": spec.name,
                "workload": workload,
                "cpu_millicores": {
                    "used": cpu_used,
                    "request": cpu_request,
                    "limit": cpu_limit,
                    "pct_of_request": percent(cpu_used, cpu_request),
                    "pct_of_limit": percent(cpu_used, cpu_limit),
                },
                "memory_mebibytes": {
                    "used": mem_used,
                    "request": mem_request,
                    "limit": mem_limit,
                    "pct_of_request": percent(mem_used, mem_request),
                    "pct_of_limit": percent(mem_used, mem_limit),
                },
                "metrics_available": bool(used),
            })

    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "source": "metrics-server (point-in-time snapshot)",
        "containers": containers,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", "--namespace", help="only collect from this namespace")
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="NAMESPACE",
        help="skip a namespace (repeatable)",
    )
    parser.add_argument("-o", "--output", help="write JSON to this file instead of stdout")
    args = parser.parse_args()

    snapshot = collect(args.namespace, set(args.exclude))
    text = json.dumps(snapshot, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(text + "\n")
        print(f"Wrote {len(snapshot['containers'])} containers to {args.output}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
