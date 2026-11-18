# Scheduling plan: `chat-serve`

> Production inference endpoint, 3 replicas with 1 H100 GPU each, highly available

**Recommended strategy:** Default kube-scheduler + spreading (Deployment, topology spread, PDB)

| field | value |
|---|---|
| kind | inference |
| workers / replicas | 3 |
| per pod | 1 GPU, 12 CPU, 100 GiB |
| GPU model | H100 |
| gang (all-or-nothing) | False |
| fast interconnect | False |
| priority | critical |

## Why
- Pin to `nvidia.com/gpu.product=NVIDIA-H100-80GB-HBM3` so the pod only lands on H100 nodes.
- Add tolerations for GPU-node taints (taints keep CPU pods off GPUs; a toleration only *allows* placement, it does not attract the pod - the nodeSelector/requests do that).
- Serving needs availability, not packing: keep the default LeastAllocated scoring so replicas spread out and a single node failure costs one replica.
- Topology spread across `topology.kubernetes.io/zone` is **hard** (DoNotSchedule) because this is critical.
- A PodDisruptionBudget keeps node drains/Karpenter consolidation from taking out several replicas at once.

## Findings
- **WARNING - High availability is not possible with the current nodes.** All eligible nodes are in ['us-east-1a']. Zone spreading cannot protect against a zone outage; add a node pool in a second zone.

## Simulation (filter -> score -> place, like kube-scheduler)
```json
{
  "single_pod_filter": "8/12 nodes are available.",
  "default_scheduler": {
    "placed": "3/3",
    "nodes": [
      "h100-a-2",
      "h100-a-2",
      "h100-a-1"
    ],
    "network_domains": [
      "block-a"
    ]
  },
  "binpack_profile": {
    "placed": "3/3",
    "nodes": [
      "h100-a-3",
      "h100-a-3",
      "h100-a-0"
    ],
    "network_domains": [
      "block-a"
    ]
  },
  "gpu_fragmentation": {
    "free_gpus": 43.0,
    "stranded_gpus": 27.0,
    "fully_free_nodes": 2,
    "stranded_ratio": 0.63
  }
}
```

## Apply
```bash
kubectl apply -f 00-priorityclass.yaml
kubectl apply -f 30-workload.yaml
```

## Alternatives considered
- Put inference on its own GPU node pool with a dedicated taint so training bin-packing never competes with it.
- If replicas need >1 GPU with NVLink (tensor parallel), request whole nodes (gpus = GPUs per node).
