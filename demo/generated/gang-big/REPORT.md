# Scheduling plan: `gang-big`

> 2 nodes x 8 H100 GPUs per node, NVLink, all-or-nothing fine-tune, 1 cpu and 1 GiB per pod

**Recommended strategy:** Secondary scheduler `gang-scheduler` (scheduler-plugins Coscheduling + GPU bin-packing)

| field | value |
|---|---|
| kind | training |
| workers / replicas | 2 |
| per pod | 8 GPU, 1 CPU, 1 GiB |
| GPU model | H100 |
| gang (all-or-nothing) | True |
| fast interconnect | True |
| priority | normal |

## Why
- Pin to `nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3` so the pod only lands on H100 nodes.
- Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only *allows* placement, it does not attract the pod - the nodeSelector/requests do that).
- No batch system is installed, so the lightest gang option is the Coscheduling plugin from kubernetes-sigs/scheduler-plugins. Pods wait at the Permit stage until `minMember` of the PodGroup can all be placed, then bind together.
- It runs as a **second** scheduler: the default scheduler is untouched (required on EKS, where the default scheduler's config cannot be edited).

## Findings
- **NOTE - Coscheduling does not wake up when GPUs free up.** The plugin retries a waiting gang when a Pod or PodGroup is added or updated, or when the scheduler's periodic retry runs (every 5 minutes). Freed GPUs alone do not trigger a retry. To retry at once, touch the PodGroup, for example with `kubectl annotate podgroup <name> retry=$(date +%s) --overwrite`.
- **NOTE - EKS: default scheduler config is not editable.** That is why the weights live in a second scheduler selected via `schedulerName`.

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "2/7 nodes are available.",
  "default_scheduler": {
    "placed": "2/2",
    "nodes": [
      "node-x",
      "node-x"
    ],
    "network_domains": [
      "block-a"
    ]
  },
  "binpack_profile": {
    "placed": "2/2",
    "nodes": [
      "node-x",
      "node-x"
    ],
    "network_domains": [
      "block-a"
    ]
  },
  "gpu_fragmentation": {
    "free_gpus": 22.0,
    "stranded_gpus": 6.0,
    "fully_free_nodes": 2,
    "stranded_ratio": 0.27
  },
  "interconnect_domains": {
    "label": "network.topology/block",
    "pods_that_fit_per_domain": {
      "block-a": 2,
      "block-b": 0
    },
    "fits_entirely_in": "block-a"
  }
}
```

## Apply
```bash
kubectl apply -f 00-priorityclass.yaml
kubectl apply -f 10-scheduler.yaml
kubectl apply -f 20-podgroup.yaml
kubectl apply -f 30-workload.yaml
```

## Alternatives considered
- Kueue: queue + quota + all-or-nothing admission; best when many teams share GPUs.
- Volcano / KAI / YuniKorn: full batch schedulers replacing the scheduling path for batch pods (gang, queues, fair share, GPU sharing); heavier to operate.
- Kubernetes 1.35 alpha Workload API (native gang scheduling in kube-scheduler); behind a feature gate, not for production yet.
- Run the descheduler (e.g. HighNodeUtilization / RemovePodsViolatingNodeAffinity) to repack drifted pods; pair with Karpenter consolidation.
