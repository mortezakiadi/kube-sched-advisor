# Contributing Guidelines

Thanks for your interest in kube-sched-advisor. This project is a prototype proposed to
Kubernetes SIG Scheduling, so it follows the Kubernetes community conventions from the start.

The project abides by the Kubernetes [Code of Conduct](code-of-conduct.md).

## Before you send a pull request

- Read the [Kubernetes Contributor Guide](https://k8s.dev/guide).
- Kubernetes projects require the [Contributor License Agreement](https://git.k8s.io/community/CLA.md).
  Once this project is hosted by a SIG, the CLA bot will check it on your pull request.
- For anything larger than a small fix, open an issue first so the approach can be discussed.

## Development setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q tests
```

Try the tool against the offline snapshots, no cluster needed:

```bash
python -m sched_advisor suggest --brief --cluster examples/cluster.yaml \
  "4 nodes x 8 H100 GPUs per node, NVLink, all-or-nothing pretraining"
```

## What a good change looks like

- **Decision rules are reviewed in code.** If you change what the advisor recommends, add or
  update a test in `tests/test_advisor.py` that shows the case.
- **Keep it read-only.** The advisor must never apply anything to a cluster.
- **Generated manifests must stay valid.** Check them with `kubeconform` or a server-side dry run.
- **Say what you did not verify.** The simulator models only the plugins listed in the README.
  Mention any behaviour you did not compare against a real kube-scheduler.

## Contact

- Slack: [#sig-scheduling](https://kubernetes.slack.com/messages/sig-scheduling)
- Mailing list: [sig-scheduling](https://groups.google.com/a/kubernetes.io/g/sig-scheduling)
