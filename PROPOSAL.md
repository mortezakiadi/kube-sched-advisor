# Proposal to SIG Scheduling: a scheduling *advisor* for AI/ML workloads

*Status: draft. Target: a kubernetes-sigs subproject under SIG Scheduling, or an extension of kube-scheduler-simulator.*

## Summary

Research teams at AI labs express needs in their own terms, for example: "64 H100s for pretraining, NVLink, by Friday." Platform engineers then translate that into Kubernetes primitives, and the decision space is large:

- requests, selectors, taints and tolerations
- topology spread
- scoring strategies and weights
- secondary scheduler profiles
- gang scheduling: Kueue, Volcano, KAI, YuniKorn, Coscheduling, or the new Workload API
- topology-aware placement

When the wrong choice is made, the symptoms are well known: `0/N nodes are available` while GPUs sit idle, half-started gangs holding GPUs, and all-reduce traffic crossing slow links.

We propose an **advisor**. It takes a natural-language ask, reads the **current cluster state**, and simulates kube-scheduler's filter/score cycle together with gang and topology feasibility. It then recommends and generates manifests, explaining each choice and the alternatives.

## Motivation

- kube-scheduler-simulator answers "where would this pod land?" Nothing answers "what should I write, and which scheduler should I use?"
- The default scheduler places pods one at a time and sees GPU *counts*, not interconnect topology. Many users only learn this after a job deadlocks or underperforms.
- Managed control planes (EKS, for example) don't allow editing the default scheduler's configuration. Users need to be steered toward secondary profiles.
- The ecosystem is fragmented (Kueue, Volcano, KAI, YuniKorn, scheduler-plugins, and the Workload API in 1.35). A neutral SIG-owned advisor can encode the trade-offs without favoring a single project.

## Goals

1. Translate natural-language asks into a typed workload spec. An LLM can do this when available, with a deterministic fallback, and the spec is always shown to the user for confirmation.
2. Read the live cluster state (nodes, allocatable resources, requests, taints, labels, installed CRDs) or an offline snapshot.
3. Explain predicted placement in kube-scheduler's own terms (`0/12 nodes are available: ...`).
4. Recommend one strategy and list the alternatives, then generate valid manifests.
5. Stay read-only. The advisor never applies anything.

## Non-goals

- Replacing any scheduler, or modeling every plugin.
- Being the source of truth for scheduling behavior. When high fidelity is needed, the advisor should delegate to kube-scheduler-simulator.

## Design

```
ask (text) ─┐
            ├─► translate ─► WorkloadAsk ─► simulate (filter/score/place, gang, topology)
cluster ────┘                                   │
                                                ▼
                         decision rules ─► Plan {strategy, rationale, findings, manifests, alternatives}
```

- **Translation.** Structured extraction through LLM tool use, grounded in a compact summary of cluster state (pools, free GPUs, taints, add-ons). A rule-based parser handles offline and CI use.
- **Simulation.** A small re-implementation of NodeResourcesFit, NodeAffinity, TaintToleration and resource scoring. It adds checks the default scheduler doesn't make: whether the whole gang fits at once, whether it fits within a single network domain, and how much GPU capacity is fragmented.
- **Decision rules.** These are explicit and reviewable (see the table in the README), so that SIG members can debate the rules in code review rather than in prompts.
- **Fidelity path (phase 2).** The generated manifests are fed to kube-scheduler-simulator's real scheduler for verification.

## Phases

| Phase | Deliverable |
|---|---|
| 0 (this prototype) | CLI with live and snapshot input, LLM or rule-based translation, Kueue/TAS, Volcano, Coscheduling, bin-pack profile, inference spread |
| 1 | Plugin for kube-scheduler-simulator (UI tab "Ask"); verification by the real scheduler |
| 2 | Workload API (1.35+) output, DRA / MIG awareness, JobSet / LeaderWorkerSet, KAI and YuniKorn |
| 3 | "Why is my pod Pending?" mode, which reverses the flow from a pending pod to a fix |

## Risks

- LLM mistranslation. Mitigations: the typed schema is shown before any manifest is generated, and the deterministic rules make the decision.
- Drift as upstream APIs evolve. Mitigations: pinned API versions, kubeconform in CI, and a versions matrix.

## Ask of the SIG

Feedback on the scope, a decision on where the code should live (new subproject or inside kube-scheduler-simulator), and a sponsor/reviewer from SIG Scheduling.
