# kube-sched-advisor

> **Status: prototype, proposed to [Kubernetes SIG Scheduling](https://github.com/kubernetes/community/tree/main/sig-scheduling).**
> No SIG has sponsored it yet. See [PROPOSAL.md](PROPOSAL.md) for scope, phases and the open questions.

**Describe a workload in plain English. The tool reads your cluster's current state and tells you how to schedule the workload.** It writes the manifests and explains each choice.

```
"4 H100 nodes for pretraining, NVLink, all-or-nothing"
          │
          ▼  1. read current status   kubectl: nodes, allocatable, pod requests, taints,
          │                           labels, installed CRDs (Kueue/Volcano/JobSet/...)
          ▼  2. translate             Claude (tool use) grounded in that status,
          │                           or an offline rule-based parser
          ▼  3. simulate              filter → score → place, like kube-scheduler,
          │                           plus gang and network-topology feasibility
          ▼  4. decide                manifest tweaks | 2nd scheduler profile |
          │                           Kueue / Volcano / Coscheduling | TAS
          ▼  5. emit                  YAML + REPORT.md (why, findings, alternatives)
```

[kube-scheduler-simulator](https://github.com/kubernetes-sigs/kube-scheduler-simulator) answers the question *"where would this pod go?"* This tool answers a different one: *"I need X. What should I write, and which scheduler should I use?"*

**Demo:** a 5-minute captioned recording on a live Amazon EKS cluster (two real A10G GPU nodes, a stand-in H100 pool, Kueue), with the transcript and generated manifests, is in [`demo/`](demo/README.md).

## Quick start

```bash
pip install pyyaml            # + `pip install anthropic` for Claude translation
export ANTHROPIC_API_KEY=...  # optional; without it the rule-based parser is used

# What does the advisor see right now?
python -m sched_advisor status

# One shot, against the live cluster (current kubectl context)
python -m sched_advisor suggest "Fine-tune 70B for the alignment team on 2 H100 nodes, NVLink, shared pool"

# Short summary instead of the full report (files are still written)
python -m sched_advisor suggest --brief "Fine-tune 70B on 2 H100 nodes, NVLink, all-or-nothing"

# Set the container image and command in the generated workload
python -m sched_advisor suggest --image nvidia/cuda:12.4.1-base-ubuntu22.04 --command "sleep infinity" "serve a chat model, 2 replicas, 1 A10G GPU each"

# Interactive session; `refresh` re-reads the cluster
python -m sched_advisor chat

# Offline against a saved snapshot (no cluster needed)
python -m sched_advisor suggest --cluster examples/cluster.yaml "4 nodes x 8 H100 GPUs per node, FSDP"
```

The output goes to `out/<workload>/`: `00-priorityclass.yaml`, `10-scheduler.yaml`, `20-*.yaml` (queue/gang objects) and `30-workload.yaml`, plus `REPORT.md`.

## What it decides

| Ask | Cluster state | Recommendation |
|---|---|---|
| Multi-node synchronous training | Kueue installed or shared pool | Suspended Indexed Job + LocalQueue/ClusterQueue (cohort fair-share, preemption); **Topology-Aware Scheduling** annotation if NVLink/IB is needed |
| Same | Volcano installed | `batch.volcano.sh` Job with `minAvailable` + Queue |
| Same | No batch add-ons (e.g. EKS) | A second scheduler `gang-scheduler` (scheduler-plugins **Coscheduling**, MostAllocated GPU weight 5) + PodGroup |
| Independent GPU batch/evals | any | Indexed Job (per-index retries) on the `gpu-binpack` profile |
| Inference | any | Default scheduler, zone/host topology spread (hard when critical), PDB |

### Findings it explains

- **Fragmentation.** It reports cases like "43 GPUs free but only 2 whole nodes", which produce `0/12 nodes are available: Insufficient nvidia.com/gpu`.
- **Gang deadlock.** It shows how many workers the default scheduler would start before getting stuck.
- **Topology split.** It flags when default spreading would put NCCL traffic across network blocks.
- **Pods that can't schedule.** It gives the exact scheduler-style reason, such as an untolerated spot taint or a label mismatch.
- **Topology mismatches.** It flags requests for HA when only one zone is eligible, and missing GPU models.
- **Managed-cluster limits.** On EKS the default scheduler's config can't be edited, so weights go in a second profile.

## Layout

```
sched_advisor/
  parser.py      plain English → WorkloadAsk (Claude tool-use with cluster context, or rules)
  cluster.py     live kubectl snapshot / YAML snapshot, summarize() for the LLM
  simulator.py   NodeResourcesFit, NodeAffinity, TaintToleration, scoring, gang + topology checks
  advisor.py     decision engine
  manifests.py   Job / Deployment / KubeSchedulerConfiguration / PodGroup / Kueue / Volcano builders
  report.py      REPORT.md + YAML writer
examples/        two snapshots + generated plans for 4 typical asks
tests/           incl. fake-kubectl live read and mocked Claude translation
```

## Versions targeted

- Kubernetes 1.33–1.35 (`kubescheduler.config.k8s.io/v1`)
- Kueue ≥ v0.15 (`kueue.x-k8s.io/v1beta2`)
- scheduler-plugins v0.33–v0.35
- Volcano `batch.volcano.sh/v1alpha1`

Generated manifests validate with kubeconform. PodGroup and Volcano objects are the exception, because no public schema exists for them.

## Limits (v0.1)

The simulator models the plugins that matter for GPU work. It does not model inter-pod affinity, preemption, DRA, or GPU sharing (MIG/time-slicing), and the advisor never applies anything itself. The 1.35 alpha Workload API is listed as an alternative but isn't generated yet.

## Community, discussion, contribution, and support

- Slack: [#sig-scheduling](https://kubernetes.slack.com/messages/sig-scheduling)
- Mailing list: [sig-scheduling](https://groups.google.com/a/kubernetes.io/g/sig-scheduling)
- Contributing: see [CONTRIBUTING.md](CONTRIBUTING.md). Security: see [SECURITY.md](SECURITY.md).

### Code of conduct

Participation in the Kubernetes community is governed by the [Kubernetes Code of Conduct](code-of-conduct.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
