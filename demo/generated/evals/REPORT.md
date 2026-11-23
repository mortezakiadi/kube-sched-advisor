# Scheduling plan: `evals`

> batch eval job with 4 workers, 1 A10G GPU each, 1 cpu and 1 GiB per pod

**Recommended strategy:** Indexed Job

| field | value |
|---|---|
| kind | batch |
| workers / replicas | 4 |
| per pod | 1 GPU, 1 CPU, 1 GiB |
| GPU model | A10G |
| gang (all-or-nothing) | False |
| fast interconnect | False |
| priority | normal |

## Why
- Pin to `nvidia.com/gpu.product=NVIDIA-A10G` so the pod only lands on A10G nodes.
- Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only *allows* placement, it does not attract the pod - the nodeSelector/requests do that).
- Pods are independent (no all-reduce), so gang semantics are unnecessary; each index can retry on its own (`backoffLimitPerIndex`).

## Findings
- **BLOCKER - No node can run even one pod.** `0/5 nodes are available: 3 node(s) didn't match Pod's node affinity/selector, 2 Insufficient nvidia.com/gpu.` Likely causes: label mismatch: check the GPU model / node pool labels; requests larger than any node's free allocatable (requests, not real usage, are what count; right-size with VPA/Goldilocks).

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "0/5 nodes are available: 3 node(s) didn't match Pod's node affinity/selector, 2 Insufficient nvidia.com/gpu.",
  "default_scheduler": {
    "placed": "0/4",
    "first_failure": "0/5 nodes are available: 3 node(s) didn't match Pod's node affinity/selector, 2 Insufficient nvidia.com/gpu."
  },
  "binpack_profile": {
    "placed": "0/4",
    "first_failure": "0/5 nodes are available: 3 node(s) didn't match Pod's node affinity/selector, 2 Insufficient nvidia.com/gpu."
  },
  "gpu_fragmentation": {
    "free_gpus": 0.0,
    "stranded_gpus": 0.0,
    "fully_free_nodes": 0,
    "stranded_ratio": 0.0
  }
}
```

## Apply
```bash
kubectl apply -f 00-priorityclass.yaml
kubectl apply -f 30-workload.yaml
```

## Alternatives considered
- Kueue: queue + quota + all-or-nothing admission; best when many teams share GPUs.
- Volcano / KAI / YuniKorn: full batch schedulers replacing the scheduling path for batch pods (gang, queues, fair share, GPU sharing); heavier to operate.
- scheduler-plugins Coscheduling (PodGroup + minMember) as a lightweight second scheduler.
- Kubernetes 1.35 alpha Workload API (native gang scheduling in kube-scheduler); behind a feature gate, not for production yet.
- Run the descheduler (e.g. HighNodeUtilization / RemovePodsViolatingNodeAffinity) to repack drifted pods; pair with Karpenter consolidation.
