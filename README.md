# Prime Handoff Script

`prime_handoff.py` provisions Prime Intellect pod(s), waits for SSH, installs missing selected coding CLIs on the remote node, copies your current project directory, and syncs coding-agent state (sessions/config/auth) so you can continue work remotely with minimal interruption.

The script is designed for local-to-Prime handoff workflows like:

1. Start coding locally.
2. Decide you want remote compute.
3. Spawn a CPU/GPU pod.
4. SSH in and continue with the same project path + agent context.

## What It Handles

- Creates CPU or GPU pods via Prime CLI.
- Waits for pod SSH details and transport readiness.
- Installs `tmux` automatically on every created pod.
- Installs selected CLIs automatically on the remote node when missing.
- Copies current working directory (`cwd`) to remote.
- Never copies `.prime-handoff/transcripts/...` from the project tree (hard excluded to avoid resume-breaking transcript artifacts).
- Syncs CLI state for selected tools:
  - `codex`
  - `kimi`
  - `opencode`
  - `pi`
  - `claude` (Claude Code)
  - `amp`
- Keeps transcript/session files only in each CLI's native storage paths to preserve resume behavior.

## Prerequisites

- Prime CLI installed and logged in:
  - `prime --version`
  - `prime whoami`
- Local SSH key configured for Prime:
  - `prime config set-ssh-key-path <private_key_path>`
  - or `PRIME_SSH_KEY_PATH` environment variable
- `rsync` and `ssh` available locally.
- Run script with `uv`:
  - `uv run prime_handoff.py --help`
- Remote pod requirements for auto-install:
  - internet access
  - apt-based image (Ubuntu images work)
  - root or `sudo` for package installs when needed
  - this is used for `tmux` install and any selected CLI installs

## Important Security Note

By default, login/auth files are copied to remote nodes so the agent CLIs can continue seamlessly.

- If you do not want that behavior, pass `--skip-auth`.
- With `--skip-auth`, you will need to login again on remote for affected CLIs.
- `--skip-auth` currently excludes:
  - Codex: `~/.codex/auth.json`
  - Kimi: `~/.kimi/config.toml` and `~/.kimi/device_id` (keeps `kimi.json` for resume mapping)
  - OpenCode: `~/.local/share/opencode/auth.json`
  - Pi: `~/.pi/agent/auth.json`
  - Claude Code: `~/.claude.json`, `~/.claude/.encryption_key`
  - Amp: no known on-disk auth file under `~/.amp`/`~/.config/amp` (tokens are typically external)

## Quick Start

Create one CPU pod and sync Codex state plus project:

```bash
uv run prime_handoff.py --kind cpu --clis codex
```

Create one H100 pod:

```bash
uv run prime_handoff.py --kind gpu --gpu-type H100_80GB --gpu-count 1 --clis codex
```

Create two CPU pods:

```bash
uv run prime_handoff.py --kind cpu --node-count 2 --clis codex
```

Show planned commands without creating/copying:

```bash
uv run prime_handoff.py --kind cpu --clis codex --dry-run
```

## Automatic CLI Installation

The script always installs selected CLIs automatically if they are missing on the target pod.

- No extra flag is needed.
- Installation is driven entirely by `--clis`.
- Current installers:
  - `codex` via npm package `@openai/codex`
  - `opencode` via npm package `opencode-ai`
  - `pi` via npm package `@mariozechner/pi-coding-agent`
  - `claude` via npm package `@anthropic-ai/claude-code`
  - `amp` via npm package `@sourcegraph/amp`
  - `kimi` via official installer `https://code.kimi.com/install.sh`
- For npm-based CLIs, Node.js/npm are bootstrapped automatically on apt-based images, then upgraded to Node.js 20 when needed.

## Codex Sync Behavior

- Codex state is synced by rsyncing the full `~/.codex` directory.
- This includes all sessions/transcripts under `~/.codex/sessions/...`.
- With `--skip-auth`, only `~/.codex/auth.json` is excluded.

## Resume-Safe Paths (Per CLI)

The script copies session/transcript data to each CLI's native locations (not to custom project-local transcript files):

- Codex:
  - Resume behavior: `codex resume` filters by `cwd` by default, `--all` disables `cwd` filtering.
  - Copied paths: `~/.codex/...` (including session files under `~/.codex/sessions`).
- Kimi:
  - Resume behavior: `--continue` and `--session` are tied to the working directory/session id.
  - Copied paths: `~/.kimi/...` including `sessions`, `kimi.json` (working-directory/session mapping), and `user-history`.
- OpenCode:
  - Resume behavior: `--continue` continues latest session, `--session <id>` continues specific session.
  - Copied paths: `~/.config/opencode/...` and `~/.local/share/opencode/...` (SQLite DB + storage + project data).
- Pi:
  - Resume behavior: `--continue`, `--resume`, `--session <path>`, optional custom `--session-dir`.
  - Copied paths: `~/.pi/agent/...` by default (`PI_CODING_AGENT_DIR`), plus optional `~/.config/pi` and `~/.local/share/pi` when present.
- Claude Code:
  - Resume behavior: `claude --continue` (current directory), `claude --resume`.
  - Copied paths: `~/.claude/...` (projects/history/transcripts/settings) and `~/.claude.json` when not using `--skip-auth`.
- Amp:
  - Resume behavior: `amp threads continue`, `amp threads list`.
  - Copied paths: `~/.amp/...` and `~/.config/amp/settings.json`.

## Path Strategy

Default behavior mirrors local absolute project path:

- local: `/Users/you/code/myproj`
- remote: `/Users/you/code/myproj`

This improves compatibility for tools that key history by project path.

You can disable this:

- `--no-mirror-cwd` -> project copied to `$HOME/<project_name>`
- `--remote-project-dir /custom/path` -> explicit target path

## CLI Reference

Run full help anytime:

```bash
uv run prime_handoff.py --help
```

Core provisioning flags:

- `--kind {cpu,gpu}`
- `--node-count <int>`
- `--cpu-type <name>`
- `--gpu-type <name>`
- `--gpu-count <int>`
- `--spot {avoid,allow,only}`
- `--provider <name>`
- `--socket <name>`
- `--region <value>` (repeatable)
- `--image <image>`
- `--disk-size <gb>`
- `--vcpus <count>`
- `--memory <gb>`
- `--team-id <id>`
- `--name-prefix <prefix>`

Readiness and timing:

- `--poll-seconds <seconds>`
- `--timeout-seconds <seconds>` for pod SSH details
- `--ssh-timeout-seconds <seconds>` for SSH transport readiness

Project sync:

- `--mirror-cwd` (default)
- `--no-mirror-cwd`
- `--remote-project-dir <path>`
- `--skip-project`
- `--project-exclude <pattern>` (repeatable)
  - `.prime-handoff/transcripts` is always excluded automatically.

Agent-state sync:

- `--clis codex,kimi,opencode,pi`
- `--clis codex,kimi,opencode,pi,claude,amp` (alias `claude-code`)
  - Missing selected CLIs are installed automatically on the remote node.
- `--skip-auth`

Execution mode:

- `--ssh` (SSH into pod at end; only valid with `--node-count 1`)
- `--dry-run`

## Tested Flow (CPU + Codex)

The script was validated against Prime CLI `0.5.40` with a real CPU pod flow:

1. Create CPU pod.
2. Wait for SSH readiness.
3. Sync project directory.
4. Sync Codex state.
5. Confirm remote project + Codex files are present.

## Typical Recipes

Minimal CPU handoff for Codex:

```bash
uv run prime_handoff.py --kind cpu --clis codex
```

CPU handoff, no auth copy:

```bash
uv run prime_handoff.py --kind cpu --clis codex --skip-auth
```

GPU handoff with provider/region filters:

```bash
uv run prime_handoff.py \
  --kind gpu \
  --gpu-type H100_80GB \
  --gpu-count 1 \
  --provider datacrunch \
  --region united_states \
  --clis codex,kimi
```

SSH into node automatically:

```bash
uv run prime_handoff.py --kind cpu --clis codex --ssh
```

## Troubleshooting

If pod creation fails:

- Run `prime availability list --output json` and inspect matching resources.
- Confirm your `--image` value is valid for the chosen resource.
- Try `--spot allow` or `--spot only` if non-spot is unavailable.

If SSH fails:

- Check key path in Prime config: `prime config view`
- Ensure private key exists locally (not only `.pub`).
- Test manually:
  - `prime pods status <pod_id>`
  - `ssh -i <private_key> -p <port> <user>@<host>`

If automatic CLI install fails:

- Confirm the pod image is apt-based Ubuntu (default `ubuntu_22_cuda_12` works).
- Confirm the pod has outbound internet access.
- If pod user is not root, confirm `sudo` exists and is usable.
- Re-run with `--dry-run` to inspect the exact install commands.

If Codex transcript was not copied:

- Ensure selected CLI includes `codex` in `--clis`.
- Verify local sessions exist under `~/.codex/sessions`.
- Re-run without `--skip-auth` if your workflow depends on auth state.

## Cleanup

Terminate pods when done:

```bash
prime pods terminate <pod_id>
```
