# Demo: kube-sched-advisor on a live Amazon EKS cluster

[`demo.mp4`](demo.mp4) (about 2.5 minutes, no narration, every step captioned) shows the advisor
reading a real EKS cluster, explaining why a GPU job cannot start, and generating manifests that
the real API server accepts. [`TRANSCRIPT.md`](TRANSCRIPT.md) holds the same commands and their
real output as text.

## What the demo shows

| Step | What happens | Why it matters |
|---|---|---|
| 1-2 | A 3-node EKS cluster. Each node advertises 8 GPUs, in two network blocks. Other teams hold 2 GPUs on every node. | 18 GPUs are free, but no node has 8 free. |
| 3 | `status` reads the live cluster through `kubectl`. | The advisor works from current state, not a description of it. |
| 4 | Ask: a 2-node, 8-GPU, all-or-nothing fine-tune. The advisor warns about fragmentation, a gang that cannot start, and no single network block with room. | Explains the problem in scheduler terms before anything is submitted. |
| 5 | A pod that wants a whole node is submitted to the default kube-scheduler. It stays `Pending` with `0/3 nodes are available: 3 Insufficient nvidia.com/gpu`. | The advisor's prediction matches what the real scheduler says. |
| 6-7 | Two nodes free up. The same ask now places 2/2 workers inside one network block. | The advice follows cluster state. |
| 8-9 | The generated manifests pass a server-side dry run. A PodGroup and a second scheduler (`gang-scheduler`) are generated. | On EKS the default scheduler cannot be reconfigured, so the plan uses a second one. |
| 10 | An inference ask gets a different answer: default scheduler, hard zone spread, PodDisruptionBudget. | The advisor does not push a gang scheduler on every workload. |

Generated output for the two asks is in [`generated/`](generated/).

## What is real and what is not

- **Real:** the EKS cluster (Kubernetes 1.37), the `kubectl` reads, the default kube-scheduler's
  verdict, and the API server's dry-run validation. Every command in the video ran for real; the
  output was captured and redacted, then drawn into the video by `render_video.py`.
- **Stand-in GPUs:** the nodes are `t3.medium` CPU instances. `setup.sh` sets an
  `nvidia.com/gpu: 8` capacity on each node's status and adds the GPU product and network-block
  labels a GPU operator would normally add. The scheduler treats this like any other resource.
- **Rule-based translation:** no `ANTHROPIC_API_KEY` was set, so the ask was parsed by the
  offline rule-based parser. The asks mention `1 cpu and 1 GiB per pod` because the demo nodes are
  small; the parser would otherwise size each pod for a real GPU node.
- **Not shown:** `scheduler-plugins` (Coscheduling) is not installed, so the gang is not actually
  started. The PodGroup is not dry-run because its CRD is not installed; only the PriorityClass,
  Service and Job are validated by the API server.
- **Version:** the README targets Kubernetes 1.33-1.35. The demo cluster ran 1.37, which EKS
  provisioned by default. The advisor read it without problems, but 1.36+ is not covered by the
  version matrix yet.

## Reproduce it

Needs `eksctl`, `kubectl`, AWS credentials for a throwaway account, Python 3.10+, Pillow and `ffmpeg`.
The cluster costs roughly 25 cents an hour (EKS control plane plus three `t3.medium` nodes, at on-demand list prices).

```bash
eksctl create cluster -f demo/cluster.yaml --kubeconfig /tmp/ksa-kubeconfig
export KUBECONFIG=/tmp/ksa-kubeconfig
bash demo/setup.sh            # fake GPUs, labels, "other teams" load
pip install pyyaml pillow
python demo/record.py         # runs every step, writes transcript.json and TRANSCRIPT.md
python demo/render_video.py   # writes demo.mp4
bash demo/teardown.sh         # deletes the cluster
```

`record.py` redacts AWS account IDs, node host names and IP addresses, and refuses to write the
transcript if anything account-shaped is left. Re-running it needs a fresh `setup.sh` first,
because step 6 deletes the other teams' pods.
