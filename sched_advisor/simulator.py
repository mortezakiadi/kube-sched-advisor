"""A small, explainable re-implementation of kube-scheduler's filter/score cycle.

It is intentionally simplified (no inter-pod affinity, no preemption) but mirrors
the plugins that matter for GPU research workloads:

  Filter : NodeResourcesFit, NodeAffinity (nodeSelector), TaintToleration
  Score  : NodeResourcesFit (LeastAllocated | MostAllocated, weighted), ImageLocality

On top of the per-pod cycle it adds two checks the default scheduler does NOT do,
which is exactly where research workloads get stuck:

  * gang feasibility   - can ALL N pods be placed at the same time?
  * topology feasibility - can they all land inside one interconnect domain?
"""
from __future__ import annotations

import copy
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .models import GPU, ClusterSnapshot, Node


@dataclass
class SimPod:
    requests: dict[str, float]
    node_selector: dict[str, str] = field(default_factory=dict)
    tolerations: list[str] = field(default_factory=list)       # taint keys tolerated
    image: str = ""


@dataclass
class ScoringStrategy:
    type: str = "LeastAllocated"                                 # or MostAllocated
    weights: dict[str, int] = field(default_factory=lambda: {"cpu": 1, "memory": 1})
    image_locality_weight: int = 1


@dataclass
class FilterResult:
    feasible: list[Node]
    reasons: Counter                                             # reason -> #nodes

    def message(self, total: int) -> str:
        if self.feasible:
            return f"{len(self.feasible)}/{total} nodes are available."
        parts = ", ".join(f"{n} {r}" for r, n in self.reasons.most_common())
        return f"0/{total} nodes are available: {parts}."


@dataclass
class PlacementResult:
    placed: list[str]                                            # node per pod, in order
    unplaced: int
    first_failure: str | None
    domains_used: set[str]

    @property
    def ok(self) -> bool:
        return self.unplaced == 0


def filter_nodes(pod: SimPod, nodes: list[Node]) -> FilterResult:
    feasible, reasons = [], Counter()
    for node in nodes:
        why = None
        for key, val in pod.node_selector.items():
            if node.labels.get(key) != val:
                why = "node(s) didn't match Pod's node affinity/selector"
                break
        if not why:
            for t in node.taints:
                if t.effect in ("NoSchedule", "NoExecute") and t.key not in pod.tolerations:
                    why = f"node(s) had untolerated taint {{{t.key}: {t.value}}}"
                    break
        if not why:
            for res, amount in pod.requests.items():
                if amount > 0 and node.free(res) < amount:
                    why = f"Insufficient {res}"
                    break
        if why:
            reasons[why] += 1
        else:
            feasible.append(node)
    return FilterResult(feasible, reasons)


def _resource_score(pod: SimPod, node: Node, strat: ScoringStrategy) -> float:
    total_w, acc = 0, 0.0
    for res, w in strat.weights.items():
        cap = node.allocatable.get(res, 0)
        if cap <= 0:
            continue
        used = node.requested.get(res, 0) + pod.requests.get(res, 0)
        frac = min(used / cap, 1.0)
        acc += w * (frac if strat.type == "MostAllocated" else 1 - frac) * 100
        total_w += w
    return acc / total_w if total_w else 0.0


def score_nodes(pod: SimPod, nodes: list[Node], strat: ScoringStrategy, all_nodes: list[Node]) -> list[tuple[Node, float]]:
    spread = sum(1 for n in all_nodes if pod.image in n.images) / max(len(all_nodes), 1)
    out = []
    for n in nodes:
        s = _resource_score(pod, n, strat)
        if pod.image and pod.image in n.images:
            s += strat.image_locality_weight * 100 * spread
        out.append((n, s))
    return sorted(out, key=lambda x: (-x[1], x[0].name))


def place_gang(pod: SimPod, replicas: int, cluster: ClusterSnapshot, strat: ScoringStrategy,
               domain_label: str | None = None, restrict_domain: str | None = None) -> PlacementResult:
    """Place `replicas` identical pods one by one, updating node usage, like the scheduler would."""
    nodes = copy.deepcopy(cluster.nodes)
    if restrict_domain and domain_label:
        nodes = [n for n in nodes if n.labels.get(domain_label) == restrict_domain]
    placed, failure = [], None
    for _ in range(replicas):
        fr = filter_nodes(pod, nodes)
        if not fr.feasible:
            failure = fr.message(len(nodes))
            break
        best, _ = score_nodes(pod, fr.feasible, strat, nodes)[0]
        for res, amt in pod.requests.items():
            best.requested[res] = best.requested.get(res, 0) + amt
        placed.append(best.name)
    domains = set()
    if domain_label:
        by_name = {n.name: n for n in cluster.nodes}
        domains = {by_name[p].labels.get(domain_label, "?") for p in placed}
    return PlacementResult(placed, replicas - len(placed), failure, domains)


def best_single_domain(pod: SimPod, replicas: int, cluster: ClusterSnapshot,
                       strat: ScoringStrategy, domain_label: str) -> tuple[str | None, dict[str, int]]:
    """Return a domain that fits the whole gang, plus capacity per domain (in pods)."""
    capacity: dict[str, int] = defaultdict(int)
    domains = sorted({n.labels[domain_label] for n in cluster.nodes if domain_label in n.labels})
    winner = None
    for d in domains:
        r = place_gang(pod, replicas, cluster, strat, domain_label, restrict_domain=d)
        capacity[d] = len(r.placed)
        if r.ok and winner is None:
            winner = d
    return winner, dict(capacity)


def fragmentation(cluster: ClusterSnapshot, resource: str = GPU, pod: SimPod | None = None) -> dict[str, float]:
    """How much free GPU capacity is stranded on partially used nodes (only nodes eligible for `pod`)."""
    gpu_nodes = [n for n in cluster.nodes if n.allocatable.get(resource, 0) > 0
                 and (pod is None or all(n.labels.get(k) == v for k, v in pod.node_selector.items()))]
    free = sum(n.free(resource) for n in gpu_nodes)
    stranded = sum(n.free(resource) for n in gpu_nodes if 0 < n.requested.get(resource, 0))
    whole_free_nodes = sum(1 for n in gpu_nodes if n.requested.get(resource, 0) == 0)
    return {"free_gpus": free, "stranded_gpus": stranded,
            "fully_free_nodes": whole_free_nodes,
            "stranded_ratio": round(stranded / free, 2) if free else 0.0}
