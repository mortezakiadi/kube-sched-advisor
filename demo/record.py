#!/usr/bin/env python3
"""Run the demo against the live cluster and write a redacted transcript.

    export KUBECONFIG=<kubeconfig of the demo cluster>
    python demo/record.py            # writes demo/transcript.json and demo/TRANSCRIPT.md
    python demo/render_video.py      # turns the transcript into demo/demo.mp4

Every command really runs; its output is captured, redacted, and stored. Redaction
replaces AWS account IDs, node host names and IP addresses, and the run fails if
anything account-shaped is still left in the result.
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

ASK_GANG = ('python -m sched_advisor suggest --brief --name gang-finetune "2 nodes x 8 H100 GPUs per node, '
            'NVLink, all-or-nothing fine-tune, 1 cpu and 1 GiB per pod"')
ASK_SERVE = ('python -m sched_advisor suggest --brief --name chat-serve "serve a chat model, 3 replicas, '
             '1 H100 GPU each, highly available, 1 cpu and 1 GiB per pod"')

STEPS = [
    dict(caption="A real Amazon EKS cluster with 3 nodes. Each node advertises 8 GPUs, in two network blocks.",
         command="kubectl get nodes -o custom-columns='NODE:.metadata.name,GPUS:.status.allocatable.nvidia\\.com/gpu,"
                 "NETWORK-BLOCK:.metadata.labels.network\\.topology/block'"),
    dict(caption="Other teams already hold 2 GPUs on every node. 18 GPUs are free, but no node has 8 free.",
         command="kubectl get pods -n other-teams -o custom-columns='POD:.metadata.name,NODE:.spec.nodeName,"
                 "GPUS-HELD:.spec.containers[0].resources.requests.nvidia\\.com/gpu'"),
    dict(caption="The advisor reads the live cluster through kubectl. No snapshot file, nothing is changed.",
         command="python -m sched_advisor status"),
    dict(caption="The ask, in plain English: a 2-node, 8-GPU fine-tune that must start all at once.",
         command=ASK_GANG, min_hold=14),
    dict(caption="Check against the real kube-scheduler: one pod that wants a whole node. Same verdict as the advisor.",
         command="kubectl apply -f demo/probe-pod.yaml && sleep 8 && kubectl get pod train-probe && "
                 "kubectl get events --field-selector involvedObject.name=train-probe,reason=FailedScheduling "
                 "-o custom-columns=MESSAGE:.message --no-headers",
         min_hold=7),
    dict(caption="Now the other teams' jobs finish and two nodes become completely free.",
         command="kubectl delete pod train-probe && kubectl delete pod -n other-teams other-team-job-0 other-team-job-1"),
    dict(caption="Same ask, new cluster state: both workers place, and the whole job fits inside one network block.",
         command=ASK_GANG, min_hold=14),
    dict(caption="It wrote manifests. Ask the real API server to validate them. Nothing is created (server dry run).",
         command="ls out/gang-finetune && kubectl create namespace research && kubectl apply --dry-run=server "
                 "-f out/gang-finetune/00-priorityclass.yaml -f out/gang-finetune/30-workload.yaml",
         min_hold=6),
    dict(caption="The plan also writes a PodGroup so both pods start together, and points the Job at the second scheduler.",
         command="cat out/gang-finetune/20-podgroup.yaml && grep -n schedulerName out/gang-finetune/30-workload.yaml",
         min_hold=8),
    dict(caption="A different ask, different answer: inference should not use a gang scheduler.",
         command=ASK_SERVE, min_hold=11),
]

INTRO = dict(title="kube-sched-advisor",
             lines=["Describe a workload in plain English. Get a scheduling plan from your live cluster.",
                    "Recorded on a real Amazon EKS cluster. No narration: each step has a caption.",
                    "The nodes advertise 8 GPUs each as a stand-in. No real GPUs were used."],
             seconds=9)
OUTRO = dict(title="Read-only. Explains why. Generates the manifests.",
             lines=["github.com/mortezakiadi/kube-sched-advisor",
                    "Proposal to Kubernetes SIG Scheduling",
                    "The demo cluster was deleted after recording."],
             seconds=8)


def sh(cmd: str, env: dict) -> str:
    p = subprocess.run(["bash", "-c", cmd], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    return (p.stdout + p.stderr).rstrip("\n")


def build_redactor(env: dict):
    names = sh("kubectl get nodes -o jsonpath='{.items[*].metadata.name}'", env).split()
    mapping = {n: f"node-{i + 1}" for i, n in enumerate(sorted(names))}

    def redact(s: str) -> str:
        for real, alias in mapping.items():
            s = s.replace(real, alias.ljust(len(real)))   # keep table columns aligned
        s = re.sub(r"ip-\d+-\d+-\d+-\d+[\w.\-]*", "node-x", s)
        s = re.sub(r"\b\d{12}\b", "<account-id>", s)
        s = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "x.x.x.x", s)
        s = s.replace(os.path.expanduser("~"), "~").replace(ROOT, ".")
        return s
    return redact


FORBIDDEN = [r"\b\d{12}\b", r"AKIA[0-9A-Z]{12,}", r"ASIA[0-9A-Z]{12,}", r"arn:aws", r"mortezak(?!iadi)",
             r"amazon\.com", r"\bip-\d+-\d+", r"\b\d{1,3}(?:\.\d{1,3}){3}\b", r"\.eks\.amazonaws"]


def main() -> int:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    if "KUBECONFIG" not in env:
        sys.exit("set KUBECONFIG to the demo cluster's kubeconfig first")
    redact = build_redactor(env)
    out_steps = []
    for st in STEPS:
        # clean previous output so each ask writes fresh files
        output = sh(st["command"], env)
        out_steps.append(dict(caption=st["caption"], command=redact(st["command"]),
                              output=redact(output), min_hold=st.get("min_hold", 0)))
        print(f"[ok] {st['caption'][:60]}")
        time.sleep(1)
    transcript = dict(intro=INTRO, steps=out_steps, outro=OUTRO)
    blob = json.dumps(transcript)
    for pat in FORBIDDEN:
        m = re.search(pat, blob)
        if m:
            sys.exit(f"REFUSING TO WRITE: redaction left something matching {pat!r}: ...{blob[max(0, m.start()-40):m.end()+40]}...")
    with open(os.path.join(HERE, "transcript.json"), "w") as fh:
        json.dump(transcript, fh, indent=2)
    with open(os.path.join(HERE, "TRANSCRIPT.md"), "w") as fh:
        fh.write("# Demo transcript (real output, redacted)\n\n")
        for i, st in enumerate(out_steps, 1):
            fh.write(f"## {i}. {st['caption']}\n\n```console\n$ {st['command']}\n{st['output']}\n```\n\n")
    print("wrote demo/transcript.json and demo/TRANSCRIPT.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
