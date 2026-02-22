# Beam

A skill to hand a current coding CLI session to a remote pod to continue experiments.
Uses [prime](https://github.com/PrimeIntellect-ai/prime) to create new pods, thus needs its CLI: `uv tool install prime`.

The script then copies over the current working directory, as well as config files (including the login) of the desired CLI(s), e.g. Codex.

It also copies over the user transcripts, so you can ssh into the created pod and then run `codex resume`.

Supported coding CLIs: codex, kimi, opencode, pi, claude, amp.

### Prerequisites
- SSH key configured for Prime (`prime config set-ssh-key-path <private_key>`)
- Local `rsync` and `ssh`
- Run with `uv` (`uv run beam.py ...`)
- Remote image must allow apt + sudo for automatic tool installs

SSH key resolution order:

1. `PRIME_SSH_KEY_PATH`
2. Prime config `ssh_key_path`
3. `~/.ssh/id_rsa`

## Project Sync Path and Excludes

Project path is always mirrored to get transcript resumption to work.

- local: `/Users/you/code/project`
- remote: `/Users/you/code/project`

Built-in excludes for project rsync:

- `node_modules`, `.venv`, `venv`, `env`
- `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`
- `dist`, `build`

## Auth Copy Behavior (`--skip-auth`)

By default, auth-related files are copied for seamless continuation.

With `--skip-auth`, these are excluded:

- Codex: `~/.codex/auth.json`
- Kimi: `~/.kimi/config.toml`, `~/.kimi/device_id`
- OpenCode: `~/.local/share/opencode/auth.json`
- Pi: `~/.pi/agent/auth.json`
- Claude: `~/.claude/.encryption_key`, `~/.claude.json`
