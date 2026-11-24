# Demo: kube-sched-advisor on a live Amazon EKS cluster

[`demo.mp4`](demo.mp4) (about 7 minutes, no narration, every step captioned) shows the advisor
reading a real EKS cluster, explaining why GPU jobs cannot start, and generating manifests. Some
plans are applied for real on real GPUs. One scenario uses stand-in nodes to show a larger
8-GPU job. [`TRANSCRIPT.md`](TRANSCRIPT.md) holds the same commands and their real output as text.

## The cluster: two kinds of GPU node, each labelled on screen

| Kind | Nodes | What they are |
|---|---|---|
| **real** | 4 x `g5.xlarge` | One real NVIDIA A10G each. The NVIDIA device plugin and GPU feature discovery are installed with helm, so GPU counts and the `nvidia.com/gpu.product` label come from real discovery. The nodes carry the usual `nvidia.com/gpu` taint. |
| **stand-in** | 3 x `t3.medium` | CPU machines with **no GPU**. `setup.sh` sets `nvidia.com/gpu: 8` on each node's status, plus the H100 product label and a network-block label. The scheduler treats that like any other capacity, so the demo can show 8-GPU, multi-node and network-block decisions that four single-GPU nodes cannot. Only no-op containers are ever placed on them. |

Every node's kind is the `KIND` column in the first step of the video. Scenarios 1, 2 and 5 use
only real GPUs. Scenario 3 uses only stand-in nodes. Scenario 4 asks for hardware neither has.

Other teams' jobs are pods that hold GPUs and run a do-nothing image. They carry a priority class
with the same value as the advisor's "normal" class, so nothing gets preempted. A pod with no
priority at all would be preempted by the advisor's workloads, which is a lever the advisor
documents, but it is not the story told here.

## Scenarios

| # | GPUs | Scenario | What it shows |
|---|---|---|---|
| 1 | real | A 3-worker, all-or-nothing training job while only 2 GPUs are free | The advisor warns that starting 2 of 3 workers would deadlock the job. The same job on the default scheduler does exactly that: two workers start and hold GPUs, the third waits. The advisor's generated second scheduler and PodGroup are applied: all three workers wait and none holds a GPU. After another team's job frees a GPU, the gang starts together and each worker reports its own A10G. |
| 2 | real | A highly available inference service | A different ask gets a different plan: the default scheduler, zone spread and a PodDisruptionBudget. It is applied for real, and `nvidia-smi` inside a pod shows the A10G. |
| 3 | stand-in | A big job: 2 nodes x 8 H100, in one network block | The advisor reports fragmentation (18 GPUs free, no node with 8) and no network block with room. A pod submitted to the real kube-scheduler stays `Pending` with `Insufficient nvidia.com/gpu`, the same verdict. After two nodes free up, the same ask fits inside one block and the generated manifests, including the PodGroup, pass a server-side dry run. This shows the scheduling decision, not a GPU workload. |
| 4 | none | Hardware the cluster does not have | An ask for B200 GPUs gets a blocker saying no B200 nodes exist. |
| 5 | real | The platform team installs Kueue | The advisor detects the Kueue API extensions and changes the plan to queues and quota. The generated queue objects pass a server-side dry run against the real Kueue API. |

Generated output for the asks is in [`generated/`](generated/).

## What the demo found in the advisor

Running the generated second scheduler on a real cluster showed gaps that dry runs and unit tests
could not. They are fixed in the same change:

- **Missing permissions.** The second scheduler could not read the API server's client-auth
  config, could not watch PodGroups, and could not create its own leader-election lease, so no
  replica ever became leader and nothing was scheduled. The advisor now generates the three
  bindings.
- **A gang does not wake up when GPUs free up.** The Coscheduling plugin registers only Pod-add
  and PodGroup events, so a waiting gang retries on a new pod, on a PodGroup change, or on the
  scheduler's 5-minute timer. The demo shows this: after a GPU is freed the gang is still waiting
  20 seconds later, and touching the PodGroup starts it within seconds. The advisor now says so in
  its findings.

## What is real and what is not

- **Real:** the EKS cluster (Kubernetes 1.37), the four A10G GPUs and their discovery, the
  `kubectl` reads, the default scheduler's behavior, the second scheduler (scheduler-plugins
  v0.34.7), the gang job, the inference Deployment, the API server's dry-run validation, and the
  Kueue install (v0.20.7). Every command in the video ran for real; the output was captured and
  redacted, then drawn into the video by `render_video.py`.
- **Not real:** the 24 GPUs on the three stand-in nodes. Scenario 3's numbers come from a real
  scheduler working on advertised capacity.
- **Edits to the plans:** `--image` and `--command` were given on the command line so the real-GPU
  pods run a CUDA base image with a trivial command. Nothing else in the generated manifests was
  changed.
- **Off screen:** installing the PodGroup API and installing Kueue are omitted from the video and
  marked as omitted in the transcript. The helm install of the device plugin is in `setup.sh`.
- **Rule-based translation:** no `ANTHROPIC_API_KEY` was set, so asks were parsed by the offline
  rule-based parser. The asks say `1 cpu and 1 GiB per pod` so that pods fit the small nodes.
- **Not covered:** Kueue's plan is validated but not run, and real multi-GPU nodes and NVLink are
  not covered. A real 4-GPU `g5.12xlarge` node was tried and AWS had no capacity for it in any
  zone of the region at the time. Larger real GPU counts also need a vCPU quota increase.
- **Version:** the README targets Kubernetes 1.33-1.35. EKS provisioned 1.37 by default. The
  advisor and scheduler-plugins v0.34.7 worked against it, but 1.36+ is not covered by the
  version matrix yet.

## Reproduce it

Needs `eksctl`, `kubectl`, `helm`, AWS credentials for a throwaway account with quota for four
`g5.xlarge` instances (16 vCPUs of the "Running On-Demand G and VT instances" quota), Python 3.10+,
Pillow and `ffmpeg`. The cluster costs roughly $5 an hour (EKS control plane, three `t3.medium`
and four `g5.xlarge` nodes, at on-demand list prices).

```bash
eksctl create cluster -f demo/cluster.yaml --install-nvidia-plugin=false --kubeconfig /tmp/ksa-kubeconfig
export KUBECONFIG=/tmp/ksa-kubeconfig
bash demo/setup.sh            # device plugin + GPU discovery, stand-in GPUs, labels, "other teams" jobs
pip install pyyaml pillow
python demo/record.py         # runs every step, writes transcript.json and TRANSCRIPT.md
python demo/render_video.py   # writes demo.mp4
bash demo/teardown.sh         # deletes the cluster
```

`record.py` redacts AWS account IDs, node host names, IP addresses and GPU UUIDs, and refuses to
write the transcript if anything account-shaped is left. Re-running it needs a fresh `setup.sh`
first, because the scenarios delete the other teams' pods and install Kueue and the PodGroup API.
