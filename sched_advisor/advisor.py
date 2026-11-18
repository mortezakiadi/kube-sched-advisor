"""Decision engine: simulate the ask against the cluster, pick a scheduling strategy,
and generate the manifests plus an explanation a researcher can read."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import manifests as M
from .models import GPU, ClusterSnapshot, gpu_model_of, Priority, WorkloadAsk, WorkloadKind
from .simulator import (PlacementResult, ScoringStrategy, SimPod, best_single_domain,
                        filter_nodes, fragmentation, place_gang)

BINPACK_SCHEDULER = "gpu-binpack"
COSCHED_SCHEDULER = "gang-scheduler"
SCHEDULER_IMAGE = "registry.k8s.io/kube-scheduler:v{ver}.0"  # match your control-plane version
# scheduler-plugins releases track k8s minors (v0.<minor>.<patch>); pin the patch you have tested.
SCHEDULER_PLUGINS_TAGS = {33: "v0.33.5", 34: "v0.34.7", 35: "v0.35.7"}
SCHEDULER_PLUGINS_IMAGE = "registry.k8s.io/scheduler-plugins/kube-scheduler:{tag}"

LEAST = ScoringStrategy("LeastAllocated", {"cpu": 1, "memory": 1})
BINPACK = ScoringStrategy("MostAllocated", {"cpu": 1, "memory": 1, GPU: 5})


@dataclass
class Finding:
    severity: str          # info | warn | block
    title: str
    detail: str


@dataclass
class Plan:
    ask: WorkloadAsk
    strategy: str
    rationale: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    files: dict[str, list[dict]] = field(default_factory=dict)
    alternatives: list[str] = field(default_factory=list)
    simulation: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
def _gpu_nodes(cluster: ClusterSnapshot):
    return [n for n in cluster.nodes if n.allocatable.get(GPU, 0) > 0]


def _placement_constraints(ask: WorkloadAsk, cluster: ClusterSnapshot, plan: Plan):
    """Work out nodeSelector + tolerations the pod needs to even be considered."""
    node_selector, tol_keys = {}, set()
    gpu_nodes = _gpu_nodes(cluster)

    if ask.gpus_per_pod and ask.gpu_model:
        values = sorted({n.labels.get(cluster.gpu_model_label, "") for n in gpu_nodes})
        match = [v for v in values if ask.gpu_model.lower() in gpu_model_of(v).lower()]
        if match:
            node_selector[cluster.gpu_model_label] = match[0]
            plan.rationale.append(f"Pin to `{cluster.gpu_model_label}={match[0]}` so the pod only lands on {ask.gpu_model} nodes.")
        else:
            plan.findings.append(Finding("block", f"No {ask.gpu_model} nodes found",
                                         f"GPU models in the cluster: {', '.join(v for v in values if v) or 'none labelled'}. "
                                         "Either relax the model or add a node pool (Karpenter NodePool / managed node group)."))

    if ask.gpus_per_pod:
        for n in gpu_nodes:
            for t in n.taints:
                if "spot" in t.key.lower() and not ask.spot_ok:
                    continue
                tol_keys.add(t.key)
        if tol_keys:
            plan.rationale.append("Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only "
                                  "*allows* placement, it does not attract the pod - the nodeSelector/requests do that).")
    elif ask.spot_ok:
        tol_keys |= {t.key for n in cluster.nodes for t in n.taints if "spot" in t.key.lower()}

    tolerations = [{"key": k, "operator": "Exists", "effect": "NoSchedule"} for k in sorted(tol_keys)]
    return node_selector, tolerations, sorted(tol_keys)


def _simulate(ask: WorkloadAsk, cluster: ClusterSnapshot, sim_pod: SimPod, plan: Plan):
    total = len(cluster.nodes)
    fr = filter_nodes(sim_pod, cluster.nodes)
    plan.simulation["single_pod_filter"] = fr.message(total)
    if not fr.feasible:
        hints = []
        if any("taint" in r for r in fr.reasons):
            hints.append("an untolerated taint: if it is a spot/preemptible taint and the job can checkpoint, say "
                         "'spot is fine'; otherwise target nodes without it")
        if any("selector" in r for r in fr.reasons):
            hints.append("label mismatch: check the GPU model / node pool labels")
        if any("Insufficient" in r for r in fr.reasons):
            hints.append("requests larger than any node's free allocatable (requests, not real usage, are what count; "
                         "right-size with VPA/Goldilocks)")
        plan.findings.append(Finding("block", "No node can run even one pod",
                                     f"`{fr.message(total)}` Likely causes: " + "; ".join(hints) + "."))

    default = place_gang(sim_pod, ask.replicas, cluster, LEAST, cluster.interconnect_label)
    binpack = place_gang(sim_pod, ask.replicas, cluster, BINPACK, cluster.interconnect_label)
    plan.simulation["default_scheduler"] = _describe(default, ask)
    plan.simulation["binpack_profile"] = _describe(binpack, ask)

    if ask.gpus_per_pod:
        frag = fragmentation(cluster, pod=sim_pod)
        plan.simulation["gpu_fragmentation"] = frag
        if (frag["free_gpus"] >= ask.total_gpus and not default.ok and frag["stranded_gpus"] > 0
                and f"Insufficient {GPU}" in (default.first_failure or "")):
            plan.findings.append(Finding(
                "warn", "Enough GPUs in total, but they are fragmented",
                f"{frag['free_gpus']:g} GPUs are free, but {frag['stranded_gpus']:g} of them sit on partially-used "
                f"nodes and each pod needs {ask.gpus_per_pod}. This is the classic '0/N nodes are available: "
                "Insufficient nvidia.com/gpu' with idle GPUs on the dashboard. Bin-packing (MostAllocated with a "
                "high GPU weight) and the descheduler fix this over time."))
        elif fr.feasible and frag["free_gpus"] < ask.total_gpus:
            plan.findings.append(Finding(
                "block", "Not enough free GPUs right now",
                f"The ask needs {ask.total_gpus} GPUs; {frag['free_gpus']:g} are free. Without a queue the pods sit "
                "Pending and hold partial capacity. Queue it (Kueue/Volcano) so it is admitted only when capacity "
                "exists, or let Karpenter/cluster-autoscaler add nodes."))

    if ask.is_gang and not default.ok and not binpack.ok:
        plan.findings.append(Finding(
            "warn", "The gang cannot start right now under any scoring strategy",
            "With gang scheduling the job simply waits (holding nothing) instead of half-starting. To start it sooner: "
            "add capacity (Karpenter NodePool / node group for this GPU type), drain stragglers off partially-used "
            "nodes (descheduler), or preempt lower-priority work via PriorityClass."))
    if ask.is_gang and not default.ok and default.placed:
        plan.findings.append(Finding(
            "warn", "Partial placement would deadlock the job",
            f"The default scheduler places pods one at a time: {len(default.placed)}/{ask.replicas} workers would "
            "start and hold GPUs while waiting for the rest, which may never fit. Gang scheduling is required."))

    domain = None
    if ask.needs_fast_interconnect and cluster.interconnect_label and ask.replicas > 1:
        domain, cap = best_single_domain(sim_pod, ask.replicas, cluster, BINPACK, cluster.interconnect_label)
        plan.simulation["interconnect_domains"] = {"label": cluster.interconnect_label,
                                                   "pods_that_fit_per_domain": cap, "fits_entirely_in": domain}
        if default.ok and len(default.domains_used) > 1:
            plan.findings.append(Finding(
                "warn", "Default placement splits the job across network domains",
                f"LeastAllocated spread the workers over {sorted(default.domains_used)}. NCCL all-reduce then crosses "
                "the slowest link. The scheduler sees GPU *counts*, not NVLink/IB topology, so this must be "
                "requested explicitly (Kueue Topology-Aware Scheduling or JobSet exclusive topology)."))
        if not domain:
            plan.findings.append(Finding(
                "warn", "No single network domain can host the whole job",
                f"Capacity per `{cluster.interconnect_label}`: {cap}. Use a *preferred* topology "
                "(best effort) or reduce the number of workers."))
    elif ask.needs_fast_interconnect and not cluster.interconnect_label:
        plan.findings.append(Finding(
            "info", "Topology labels missing",
            "You asked for fast interconnect but nodes carry no network-topology label. Label nodes by rack/block "
            "(cloud providers expose e.g. `cloud.google.com/gce-topology-block` or EC2 `topology.k8s.aws/network-node-layer-*`)."))
    return default, binpack, domain


def _describe(r: PlacementResult, ask: WorkloadAsk) -> dict:
    out = {"placed": f"{len(r.placed)}/{ask.replicas}"}
    if r.placed:
        out["nodes"] = r.placed
    if r.domains_used:
        out["network_domains"] = sorted(r.domains_used)
    if r.first_failure:
        out["first_failure"] = r.first_failure
    return out


def _ver(cluster: ClusterSnapshot) -> tuple[int, int]:
    major, minor = cluster.k8s_version.split(".")[:2]
    return int(major), int(minor)


# ---------------------------------------------------------------------------
def advise(ask: WorkloadAsk, cluster: ClusterSnapshot) -> Plan:
    plan = Plan(ask=ask, strategy="")
    node_selector, tolerations, tol_keys = _placement_constraints(ask, cluster, plan)
    sim_pod = SimPod(requests={"cpu": ask.cpu_per_pod, "memory": ask.memory_gib_per_pod, GPU: ask.gpus_per_pod},
                     node_selector=node_selector, tolerations=tol_keys, image=ask.image)
    default, binpack, domain = _simulate(ask, cluster, sim_pod, plan)

    files: dict[str, list[dict]] = {"00-priorityclass.yaml": [M.priority_class(ask.priority)]}
    _, minor = _ver(cluster)
    sched_image = SCHEDULER_IMAGE.format(ver=cluster.k8s_version)

    # ------------------------------------------------------------- inference
    if ask.kind in (WorkloadKind.INFERENCE, WorkloadKind.INTERACTIVE):
        hard = ask.kind == WorkloadKind.INFERENCE and ask.spread_across_zones
        plan.strategy = "Default kube-scheduler + spreading (Deployment, topology spread, PDB)"
        plan.rationale += [
            "Serving needs availability, not packing: keep the default LeastAllocated scoring so replicas spread out "
            "and a single node failure costs one replica.",
            f"Topology spread across `{cluster.zone_label}` is "
            + ("**hard** (DoNotSchedule) because this is critical." if hard else "soft (ScheduleAnyway)."),
            "A PodDisruptionBudget keeps node drains/Karpenter consolidation from taking out several replicas at once.",
        ]
        eligible = filter_nodes(SimPod({}, node_selector, tol_keys), cluster.nodes).feasible
        zones = sorted({n.labels.get(cluster.zone_label, "?") for n in eligible})
        if ask.spread_across_zones and len(zones) < 2 and eligible:
            plan.findings.append(Finding("warn", "High availability is not possible with the current nodes",
                                         f"All eligible nodes are in {zones}. Zone spreading cannot protect against a "
                                         "zone outage; add a node pool in a second zone."))
        spec = M.pod_spec(ask, node_selector=node_selector, tolerations=tolerations)
        files["30-workload.yaml"] = [M.deployment(ask, spec, cluster.zone_label, hard)]
        if ask.replicas > 1:
            files["30-workload.yaml"].append(M.pdb(ask))
        plan.alternatives += [
            "Put inference on its own GPU node pool with a dedicated taint so training bin-packing never competes with it.",
            "If replicas need >1 GPU with NVLink (tensor parallel), request whole nodes (gpus = GPUs per node).",
        ]
        plan.files = files
        return plan

    # ------------------------------------------------------- training / batch
    gang = ask.is_gang
    want_binpack = bool(ask.gpus_per_pod) and (
        (not default.ok and binpack.ok)
        or plan.simulation.get("gpu_fragmentation", {}).get("stranded_ratio", 0) >= 0.3
        or ask.kind == WorkloadKind.TRAINING)
    use_kueue = gang and (cluster.has("kueue") or ask.queue_fair_share)
    use_volcano = gang and not use_kueue and cluster.has("volcano")
    use_cosched = gang and not use_kueue and not use_volcano

    scheduler_name = None
    if use_cosched:
        scheduler_name = COSCHED_SCHEDULER
        cfg = M.scheduler_config(COSCHED_SCHEDULER, strategy="MostAllocated", weights=BINPACK.weights,
                                 coscheduling=True, image_locality_weight=1)
        files["10-scheduler.yaml"] = M.scheduler_deployment(
            COSCHED_SCHEDULER, cfg, SCHEDULER_PLUGINS_IMAGE.format(tag=SCHEDULER_PLUGINS_TAGS.get(minor, "v0.34.7")))
        files["20-podgroup.yaml"] = [M.pod_group(ask)]
    elif want_binpack and not use_volcano:
        scheduler_name = BINPACK_SCHEDULER
        cfg = M.scheduler_config(BINPACK_SCHEDULER, strategy="MostAllocated", weights=BINPACK.weights,
                                 image_locality_weight=1)
        files["10-scheduler.yaml"] = M.scheduler_deployment(BINPACK_SCHEDULER, cfg, sched_image)

    spec = M.pod_spec(ask, node_selector=node_selector, tolerations=tolerations, scheduler_name=scheduler_name)

    if use_kueue:
        tas = bool(ask.needs_fast_interconnect and cluster.interconnect_label)
        gpu_alloc = sum(n.allocatable.get(GPU, 0) for n in _gpu_nodes(cluster)) or ask.total_gpus
        cpu_alloc = sum(n.allocatable.get("cpu", 0) for n in _gpu_nodes(cluster)) or ask.cpu_per_pod * ask.replicas
        mem_alloc = sum(n.allocatable.get("memory", 0) for n in _gpu_nodes(cluster)) or ask.memory_gib_per_pod * ask.replicas
        flavor = (ask.gpu_model or "gpu").lower()
        files["20-kueue.yaml"] = M.kueue_objects(
            ask, flavor=flavor, node_labels=node_selector, gpus=int(gpu_alloc), cpus=cpu_alloc, mem_gib=mem_alloc,
            topology_levels=[cluster.interconnect_label, "kubernetes.io/hostname"] if tas else None,
            tolerations=tolerations)
        pod_ann = None
        if tas:
            key = "required" if domain else "preferred"
            pod_ann = {f"kueue.x-k8s.io/podset-{key}-topology": cluster.interconnect_label}
        files["30-workload.yaml"] = [
            M.headless_service(ask),
            M.indexed_job(ask, spec, labels={"kueue.x-k8s.io/queue-name": f"{ask.team}-queue"},
                          pod_annotations=pod_ann, suspend=True),
        ]
        plan.strategy = "Kueue admission (all-or-nothing quota)" + (" + Topology-Aware Scheduling" if tas else "") \
                        + (f" + `{BINPACK_SCHEDULER}` bin-packing profile" if scheduler_name else "")
        plan.rationale += [
            "Kueue keeps the Job suspended until quota for **all** workers is available, then admits it as a unit. "
            "No half-started jobs holding GPUs.",
            "A ClusterQueue per team inside a shared cohort gives fair sharing: idle quota is borrowable and reclaimed "
            "with preemption when its owner needs it back.",
        ]
        if tas:
            plan.rationale.append(
                f"Topology-Aware Scheduling: the pod-set annotation asks Kueue to place every worker inside one "
                f"`{cluster.interconnect_label}` ({'required - simulation found ' + domain + ' fits' if domain else 'preferred - no single domain fits today'}).")
    elif use_volcano:
        files["20-volcano-queue.yaml"] = [M.volcano_queue(ask, ask.total_gpus)]
        files["30-workload.yaml"] = [M.volcano_job(ask, spec)]
        plan.strategy = "Volcano gang scheduling (vcjob with minAvailable)"
        plan.rationale.append("Volcano is installed: its Job CRD with `minAvailable` gives native gang semantics, "
                              "queues, and job-level restart on worker failure.")
    elif use_cosched:
        files["30-workload.yaml"] = [
            M.headless_service(ask),
            M.indexed_job(ask, spec, pod_labels={"scheduling.x-k8s.io/pod-group": ask.name}),
        ]
        plan.strategy = f"Secondary scheduler `{COSCHED_SCHEDULER}` (scheduler-plugins Coscheduling + GPU bin-packing)"
        plan.rationale += [
            "No batch system is installed, so the lightest gang option is the Coscheduling plugin from "
            "kubernetes-sigs/scheduler-plugins. Pods wait at the Permit stage until `minMember` of the PodGroup "
            "can all be placed, then bind together.",
            "It runs as a **second** scheduler: the default scheduler is untouched (required on EKS, where the "
            "default scheduler's config cannot be edited).",
        ]
    else:
        files["30-workload.yaml"] = [M.indexed_job(ask, spec)]
        plan.strategy = "Indexed Job" + (f" on `{BINPACK_SCHEDULER}` (MostAllocated, GPU weight 5)" if scheduler_name else "")
        plan.rationale.append("Pods are independent (no all-reduce), so gang semantics are unnecessary; each index can "
                              "retry on its own (`backoffLimitPerIndex`).")

    if scheduler_name and not use_cosched:
        plan.rationale.append(
            f"`{BINPACK_SCHEDULER}` uses MostAllocated with `nvidia.com/gpu` weight 5: fill GPU nodes before touching "
            "empty ones, so whole nodes stay free for big jobs and idle nodes can be scaled down. Trade-off: larger "
            "blast radius per node, which is why it is a separate profile and services stay on the default scheduler.")
    if scheduler_name and cluster.managed == "eks":
        plan.findings.append(Finding("info", "EKS: default scheduler config is not editable",
                                     "That is why the weights live in a second scheduler selected via `schedulerName`."))

    if ask.priority == Priority.LOW:
        plan.rationale.append("Low priority with `preemptionPolicy: Never`: the job never evicts others, and can be "
                              "preempted itself - make sure it checkpoints.")

    # alternatives the SIG proposal should show side by side
    if not use_kueue:
        plan.alternatives.append("Kueue: queue + quota + all-or-nothing admission; best when many teams share GPUs.")
    if not use_volcano:
        plan.alternatives.append("Volcano / KAI / YuniKorn: full batch schedulers replacing the scheduling path for "
                                 "batch pods (gang, queues, fair share, GPU sharing); heavier to operate.")
    if not use_cosched:
        plan.alternatives.append("scheduler-plugins Coscheduling (PodGroup + minMember) as a lightweight second scheduler.")
    if minor >= 35:
        plan.alternatives.append("Kubernetes 1.35 alpha Workload API (native gang scheduling in kube-scheduler); "
                                 "behind a feature gate, not for production yet.")
    if ask.gpus_per_pod:
        plan.alternatives.append("Run the descheduler (e.g. HighNodeUtilization / RemovePodsViolatingNodeAffinity) to "
                                 "repack drifted pods; pair with Karpenter consolidation.")
    plan.files = files
    return plan
