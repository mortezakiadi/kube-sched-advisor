#!/usr/bin/env python3
"""Run the demo against the live cluster and write a redacted transcript.

    export KUBECONFIG=<kubeconfig of the demo cluster>
    bash demo/setup.sh
    python demo/record.py            # writes demo/transcript.json and demo/TRANSCRIPT.md
    python demo/render_video.py      # turns the transcript into demo/demo.mp4

Every command really runs; its output is captured, redacted, and stored. Redaction
replaces AWS account IDs, node host names and IP addresses, and the run fails if
anything account-shaped is still left in the result.

A step with hidden=True runs but is not shown in the video; it appears in TRANSCRIPT.md
marked as omitted, so nothing is done off the record.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

KUEUE_VERSION = "v0.20.7"
SCHED_PLUGINS_VERSION = "v0.34.7"
CUDA = "nvidia/cuda:12.4.1-base-ubuntu22.04"

ADVISOR = "python -m sched_advisor suggest --brief"
ASK_GANG = ADVISOR + (f' --name gang-train --image {CUDA} --command "nvidia-smi -L" '
                      '"train on 3 workers, 1 A10G GPU each, all-or-nothing, 1 cpu and 1 GiB per pod"')
ASK_GANG_KUEUE = ADVISOR + (' --name gang-kueue "train on 3 workers, 1 A10G GPU each, all-or-nothing, '
                            'shared pool with other teams, 1 cpu and 1 GiB per pod"')
ASK_SERVE = ADVISOR + (f' --name gpu-serve --image {CUDA} --command "sleep infinity" '
                       '"serve a chat model, 2 replicas, 1 A10G GPU each, highly available, 1 cpu and 1 GiB per pod"')
ASK_B200 = ADVISOR + ' --name train-b200 "train on 2 nodes with 8 B200 GPUs per node, all-or-nothing"'

ROWS_PODS = "custom-columns='POD:.metadata.name,STATUS:.status.phase,NODE:.spec.nodeName'"

STEPS = [
    # ---------------------------------------------------------------- the cluster
    dict(section=("The cluster", "A real EKS cluster. All 4 GPUs are real NVIDIA A10G."),
         caption="Four GPU nodes with one real NVIDIA A10G GPU each.",
         command="kubectl get nodes -l ksa/real-gpu=true -o custom-columns='NODE:.metadata.name,"
                 "TYPE:.metadata.labels.node\\.kubernetes\\.io/instance-type,GPUS:.status.allocatable.nvidia\\.com/gpu,"
                 "GPU-PRODUCT:.metadata.labels.nvidia\\.com/gpu\\.product'"),
    dict(caption="Other teams already run jobs on 2 of the 4 GPUs. Two are free, on two different nodes.",
         command="kubectl get pods -n other-teams -o custom-columns='POD:.metadata.name,NODE:.spec.nodeName,"
                 "GPUS-HELD:.spec.containers[0].resources.requests.nvidia\\.com/gpu'"),
    dict(caption="The advisor reads the live cluster through kubectl. Nothing is changed.",
         command="python -m sched_advisor status"),

    # ---------------------------------------------------------------- scenario 1
    dict(section=("Scenario 1: a training job that must start all at once",
                  "3 workers, 1 GPU each. Only 2 GPUs are free."),
         caption="The ask, in plain English. The advisor warns that starting 2 of 3 workers would deadlock the job.",
         command=ASK_GANG, min_hold=14),
    dict(caption="The same job on the default scheduler, for real: two workers start and hold GPUs. The third waits.",
         command="kubectl apply -f demo/plain-job.yaml && sleep 15 && kubectl get pods -l job-name=plain-train -o " + ROWS_PODS,
         min_hold=8),
    dict(caption="Delete it. Those two GPUs were held for a job that could never finish.",
         command="kubectl delete job plain-train --wait=true"),
    dict(caption="Apply what the advisor generated: a second scheduler with the Coscheduling plugin, and a PodGroup for the job.",
         command="kubectl create namespace research && kubectl apply -f out/gang-train/00-priorityclass.yaml "
                 "-f out/gang-train/10-scheduler.yaml -f out/gang-train/20-podgroup.yaml && "
                 "kubectl rollout status -n kube-system deploy/gang-scheduler --timeout=180s",
         hidden_before=dict(note=f"Installed the PodGroup API from scheduler-plugins {SCHED_PLUGINS_VERSION} (output omitted from the video)",
                            command=f"kubectl apply -f https://raw.githubusercontent.com/kubernetes-sigs/scheduler-plugins/"
                                    f"{SCHED_PLUGINS_VERSION}/manifests/coscheduling/crd.yaml"),
         min_hold=7),
    dict(caption="Submit the job. All three workers wait, and none holds a GPU. Two are free, three are needed.",
         command="kubectl apply -f out/gang-train/30-workload.yaml && sleep 25 && kubectl get pods -n research -o " + ROWS_PODS,
         min_hold=8),
    dict(caption="Another team's job finishes and frees one GPU.",
         command="kubectl delete pod -n other-teams other-team-b"),
    dict(caption="The gang scheduler retries on new pods and on a 5-minute timer, not when a GPU frees up. Still waiting.",
         command="sleep 20 && kubectl get pods -n research -o " + ROWS_PODS,
         min_hold=7),
    dict(caption="As the advisor's note says: touch the PodGroup and the scheduler retries at once.",
         command="kubectl annotate podgroup -n research gang-train retry=$(date +%s) --overwrite"),
    dict(caption="All three workers start together, each on its own real GPU.",
         command="kubectl wait --for=condition=complete job/gang-train -n research --timeout=300s && "
                 "kubectl logs -n research -l job-name=gang-train --prefix",
         min_hold=9),

    # ---------------------------------------------------------------- scenario 2
    dict(section=("Scenario 2: a highly available inference service",
                  "2 replicas, 1 GPU each. A different ask gets a different plan, applied for real."),
         caption="A serving ask. No gang scheduler this time: the default scheduler, zone spread and a disruption budget.",
         command=ASK_SERVE, min_hold=12),
    dict(caption="Apply the generated manifests for real. The only edits are the image and command given on the command line.",
         command="kubectl apply -f out/gpu-serve/00-priorityclass.yaml -f out/gpu-serve/30-workload.yaml && "
                 "kubectl rollout status -n research deploy/gpu-serve --timeout=300s",
         min_hold=6),
    dict(caption="Both replicas are running, each on its own node.",
         command="kubectl get pods -n research -l app=gpu-serve -o " + ROWS_PODS,
         min_hold=6),
    dict(caption="Inside a pod: the real GPU the scheduler gave it.",
         command="kubectl exec -n research deploy/gpu-serve -- nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv",
         min_hold=7),

    # ---------------------------------------------------------------- scenario 3
    dict(section=("Scenario 3: asking for hardware the cluster does not have",
                  "The advisor should say so instead of generating a job that never starts."),
         caption="An ask for B200 GPUs. This cluster has A10G GPUs only.",
         command=ASK_B200, min_hold=12),

    # ---------------------------------------------------------------- scenario 4
    dict(section=("Scenario 4: the platform team installs Kueue",
                  "The advisor notices and changes its plan."),
         caption="Kueue is installed on the cluster. The advisor detects it from the cluster's API extensions.",
         command="kubectl get pods -n kueue-system && kubectl get crd clusterqueues.kueue.x-k8s.io",
         hidden_before=dict(note=f"Installed Kueue {KUEUE_VERSION} (output omitted from the video)",
                            command=f"kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/"
                                    f"{KUEUE_VERSION}/manifests.yaml && kubectl wait --for=condition=Available "
                                    f"deploy/kueue-controller-manager -n kueue-system --timeout=240s"),
         min_hold=6),
    dict(caption="A shared-pool ask on the same cluster. The plan now uses Kueue queues.",
         command=ASK_GANG_KUEUE, min_hold=14),
    dict(caption="Kueue is really installed, so its API server can validate the generated queue objects. Nothing is created.",
         command="kubectl apply --dry-run=server -f out/gang-kueue/20-kueue.yaml -f out/gang-kueue/30-workload.yaml",
         min_hold=7),
]

INTRO = dict(title="kube-sched-advisor",
             lines=["Describe a workload in plain English. Get a scheduling plan from your live cluster.",
                    "Recorded on a real Amazon EKS cluster. No narration: each step has a caption.",
                    "Every GPU is real: 4 NVIDIA A10G, one on each of four nodes."],
             seconds=9)
OUTRO = dict(title="Read-only. Explains why. Generates the manifests.",
             lines=["github.com/mortezakiadi/kube-sched-advisor",
                    "Proposal to Kubernetes SIG Scheduling",
                    "The demo cluster was deleted after recording."],
             seconds=8)


def sh(cmd: str, env: dict, timeout: int = 600) -> str:
    p = subprocess.run(["bash", "-c", cmd], cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    return (p.stdout + p.stderr).rstrip("\n")


def build_redactor(env: dict):
    names = sh("kubectl get nodes -o jsonpath='{.items[*].metadata.name}'", env).split()
    real = set(sh("kubectl get nodes -l ksa/real-gpu=true -o jsonpath='{.items[*].metadata.name}'", env).split())
    mapping = {}
    for kind, group in (("gpu-node", sorted(n for n in names if n in real)),
                        ("node", sorted(n for n in names if n not in real))):
        for i, n in enumerate(group):
            mapping[n] = f"{kind}-{i + 1}"

    def redact(s: str) -> str:
        for real_name, alias in mapping.items():
            s = s.replace(real_name, alias.ljust(len(real_name)))   # keep table columns aligned
        s = re.sub(r"UUID: GPU-[0-9a-f-]+", "UUID: GPU-<id>", s)
        s = re.sub(r"ip-\d+-\d+-\d+-\d+[\w.\-]*", "node-x", s)
        s = re.sub(r"\b\d{12}\b", "<account-id>", s)
        s = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "x.x.x.x", s)
        s = s.replace(os.path.expanduser("~"), "~").replace(ROOT, ".")
        return s
    return redact


FORBIDDEN = [r"\b\d{12}\b", r"AKIA[0-9A-Z]{12,}", r"ASIA[0-9A-Z]{12,}", r"arn:aws", r"mortezak(?!iadi)",
             r"amazon\.com", r"\bip-\d+-\d+", r"\b\d{1,3}(?:\.\d{1,3}){3}\b", r"\.eks\.amazonaws",
             r"GPU-[0-9a-f]{8}-"]


def main() -> int:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    if "KUBECONFIG" not in env:
        sys.exit("set KUBECONFIG to the demo cluster's kubeconfig first")
    redact = build_redactor(env)
    out_steps, omitted = [], []
    for st in STEPS:
        pre = st.get("hidden_before")
        if pre:
            sh(pre["command"], env, timeout=900)
            omitted.append((len(out_steps), pre["note"]))
            print(f"[ok, hidden] {pre['note']}")
        output = sh(st["command"], env, timeout=900)
        step = dict(caption=st["caption"], command=redact(st["command"]), output=redact(output),
                    min_hold=st.get("min_hold", 0))
        if st.get("section"):
            step["section"] = dict(title=st["section"][0], subtitle=st["section"][1])
        out_steps.append(step)
        print(f"[ok] {st['caption'][:60]}")
        time.sleep(1)
    transcript = dict(intro=INTRO, steps=out_steps, outro=OUTRO)
    blob = json.dumps(transcript)
    for pat in FORBIDDEN:
        m = re.search(pat, blob)
        if m:
            sys.exit(f"REFUSING TO WRITE: redaction left something matching {pat!r}: "
                     f"...{blob[max(0, m.start() - 40):m.end() + 40]}...")
    with open(os.path.join(HERE, "transcript.json"), "w") as fh:
        json.dump(transcript, fh, indent=2)
    with open(os.path.join(HERE, "TRANSCRIPT.md"), "w") as fh:
        fh.write("# Demo transcript (real output, redacted)\n\n")
        for i, st in enumerate(out_steps, 1):
            for idx, note in omitted:
                if idx == i - 1:
                    fh.write(f"> Omitted from the video: {note}.\n\n")
            if st.get("section"):
                fh.write(f"# {st['section']['title']}\n\n_{st['section']['subtitle']}_\n\n")
            fh.write(f"## {i}. {st['caption']}\n\n```console\n$ {st['command']}\n{st['output']}\n```\n\n")
    print("wrote demo/transcript.json and demo/TRANSCRIPT.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
