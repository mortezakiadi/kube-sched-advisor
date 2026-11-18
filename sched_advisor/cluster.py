"""Load a ClusterSnapshot from a compact YAML file or from live `kubectl` JSON."""
from __future__ import annotations

import json
import re
import subprocess

import yaml

from .models import GPU, GPU_MODEL_LABELS, ClusterSnapshot, Node, Taint, gpu_model_of

# Well-known node labels that describe network locality (rack / block / NVLink domain).
TOPOLOGY_LABELS = [
    "cloud.google.com/gce-topology-block", "cloud.google.com/gce-topology-subblock",
    "topology.k8s.aws/network-node-layer-3", "topology.k8s.aws/network-node-layer-2",
    "nvidia.com/gpu.clique", "network.topology/block", "topology.kubernetes.io/rack",
]

_SUFFIX = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def qty(value, resource: str) -> float:
    """Parse a Kubernetes quantity. cpu -> cores, memory -> GiB, others -> count."""
    s = str(value)
    if resource == "cpu":
        return float(s[:-1]) / 1000 if s.endswith("m") else float(s)
    m = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", s)
    if not m:
        return 0.0
    num, suf = float(m.group(1)), m.group(2)
    if resource == "memory":
        return num * _SUFFIX.get(suf, 1) / 2**30
    return num


def _norm(res: dict) -> dict[str, float]:
    return {k: qty(v, k) for k, v in (res or {}).items() if k in ("cpu", "memory", GPU)}


def load_yaml(path: str) -> ClusterSnapshot:
    with open(path) as fh:
        d = yaml.safe_load(fh)
    nodes = []
    for group in d["nodes"]:
        count = group.get("count", 1)
        for i in range(count):
            name = group["name"] if count == 1 else f"{group['name']}-{i}"
            labels = {k: str(v).format(i=i) for k, v in group.get("labels", {}).items()}
            used = group.get("requested", {})
            if isinstance(used, list):                  # per-instance usage
                used = used[i] if i < len(used) else {}
            nodes.append(Node(
                name=name,
                allocatable=_norm(group["allocatable"]),
                requested=_norm(used),
                labels={"kubernetes.io/hostname": name, **labels},
                taints=[Taint(**t) for t in group.get("taints", [])],
                images=group.get("images", []),
            ))
    return ClusterSnapshot(nodes=nodes, **{k: v for k, v in d.items() if k != "nodes"})


def load_live(context: str | None = None) -> ClusterSnapshot:
    """Build a snapshot from the current kubeconfig (requires kubectl)."""
    base = ["kubectl"] + (["--context", context] if context else [])
    nodes_json = json.loads(subprocess.check_output(base + ["get", "nodes", "-o", "json"]))
    pods_json = json.loads(subprocess.check_output(
        base + ["get", "pods", "-A", "-o", "json", "--field-selector=status.phase!=Succeeded,status.phase!=Failed"]))
    requested: dict[str, dict[str, float]] = {}
    for p in pods_json["items"]:
        node = p["spec"].get("nodeName")
        if not node:
            continue
        acc = requested.setdefault(node, {})
        for c in p["spec"]["containers"]:
            for k, v in _norm(c.get("resources", {}).get("requests", {})).items():
                acc[k] = acc.get(k, 0) + v
    nodes = []
    for n in nodes_json["items"]:
        name = n["metadata"]["name"]
        nodes.append(Node(
            name=name,
            allocatable=_norm(n["status"]["allocatable"]),
            requested=requested.get(name, {}),
            labels=n["metadata"].get("labels", {}),
            taints=[Taint(t["key"], t.get("value", ""), t["effect"]) for t in n["spec"].get("taints", [])],
            images=[nm for img in n["status"].get("images", []) for nm in img.get("names", [])],
        ))
    installed = []
    crds = subprocess.run(base + ["get", "crd", "-o", "name"], capture_output=True, text=True).stdout
    for comp, marker in [("kueue", "kueue.x-k8s.io"), ("volcano", "volcano.sh"),
                         ("jobset", "jobset.x-k8s.io"), ("scheduler-plugins", "scheduling.x-k8s.io"),
                         ("karpenter", "karpenter.sh")]:
        if marker in crds:
            installed.append(comp)
    topo = next((lbl for lbl in TOPOLOGY_LABELS
                 if any(lbl in n.labels for n in nodes)), None)
    version = json.loads(subprocess.check_output(base + ["version", "-o", "json"]))["serverVersion"]
    managed = "eks" if "eks" in version.get("gitVersion", "") else \
              "gke" if "gke" in version.get("gitVersion", "") else None
    gpu_nodes = [n for n in nodes if n.allocatable.get(GPU, 0) > 0]
    gpu_label = next((lbl for lbl in GPU_MODEL_LABELS if gpu_nodes and all(lbl in n.labels for n in gpu_nodes)),
                     GPU_MODEL_LABELS[0])
    return ClusterSnapshot(nodes=nodes, installed=installed, managed=managed, interconnect_label=topo,
                           gpu_model_label=gpu_label,
                           k8s_version=f"{version['major']}.{version['minor'].rstrip('+')}")


def summarize(c: ClusterSnapshot) -> str:
    """Compact, LLM-friendly description of the current cluster state."""
    pools: dict[tuple, dict] = {}
    for n in c.nodes:
        raw = n.labels.get(c.gpu_model_label, "")
        model = f"{gpu_model_of(raw).upper()} ({raw})" if n.allocatable.get(GPU, 0) and raw else (raw or "cpu-only")
        key = (model, n.labels.get(c.zone_label, "?"),
               tuple(sorted(t.key for t in n.taints)))
        p = pools.setdefault(key, {"nodes": 0, "gpus_per_node": n.allocatable.get(GPU, 0),
                                   "free_gpus": 0.0, "fully_free_nodes": 0, "free_cpu": 0.0})
        p["nodes"] += 1
        p["free_gpus"] += n.free(GPU)
        p["free_cpu"] += n.free("cpu")
        p["fully_free_nodes"] += int(n.requested.get(GPU, 0) == 0 and n.allocatable.get(GPU, 0) > 0)
    lines = [f"Kubernetes {c.k8s_version}" + (f" on {c.managed.upper()}" if c.managed else ""),
             f"Installed add-ons: {', '.join(c.installed) or 'none'}",
             f"Network topology label: {c.interconnect_label or 'none'}", "Node pools:"]
    for (model, zone, taints), p in pools.items():
        lines.append(f"- {p['nodes']}x {model} in {zone}: {p['gpus_per_node']:g} GPU/node, "
                     f"{p['free_gpus']:g} GPUs free, {p['fully_free_nodes']} fully-free nodes, "
                     f"{p['free_cpu']:g} CPU free, taints={list(taints) or 'none'}")
    return "\n".join(lines)
