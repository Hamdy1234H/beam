---
name: beam
description: Use this skill to move an active local coding session and directory onto a new remote machine. This remote machine can also have GPUs.
---
# Beam Handoff Skill

Use this skill to move an active agentic local coding session to a new remote machine. This skill also installs the given CLI(s) on said machine, as well as tmux and uv.

## Goal

- Create pod(s)
- Ensure remote tooling (`tmux`, `uv`, selected coding CLIs)
- Mirror project path
- Sync CLI state
- Return SSH command + remote directory

## Primary Command

```bash
uv run beam.py --kind cpu --clis codex --node-count 1
```

## High-Value Parameters

Most important params:

- `--kind {cpu,gpu}`
- `--node-count`
- `--cpu-type` (used for CPU mode, default `CPU_NODE`)
- `--gpu-type` (default `H100_80GB`)
- `--gpu-count`
- `--region` (repeat)
- `--image`
- `--disk-size`
- `--memory`
- `--team-id`

Sync:

- `--clis` (csv: `codex,kimi,opencode,pi,claude,amp`)
- `--skip-auth` (does not copy over auth tokens, need to re-auth in ssh session)

More params can be found with `uv run beam.py --help`.

## Common Recipes

CPU handoff:

```bash
uv run beam.py --kind cpu --clis codex
```

GPU handoff:

```bash
uv run beam.py --kind gpu --gpu-type H100_80GB --gpu-count 4 --clis codex
```

Small custom CPU pod:

```bash
uv run beam.py --kind cpu --vcpus 4 --memory 16 --disk-size 200 --clis codex
```

## Execution Checklist

1. Confirm Prime is authenticated (`prime whoami`).
2. Run `uv run beam.py ...` with desired params.
3. Wait for summary output.
4. Return the summary to the user.
