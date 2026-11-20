## What changed

## Why

## How it was checked
- [ ] `python -m pytest -q tests` passes
- [ ] If a recommendation changed: a test shows the case
- [ ] If manifests changed: validated with kubeconform or a server-side dry run
- [ ] Anything not verified against a real kube-scheduler is called out above
