#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10,<3.14"
# ///
"""Provision Prime pod(s) and hand off local coding-agent state.

This script creates one or more Prime Intellect pods (CPU or GPU), waits for SSH,
copies the current working directory, and syncs local CLI state for supported
coding agents (Codex, Kimi, OpenCode, Pi, Claude Code, Amp).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

SUPPORTED_CLIS = ("codex", "kimi", "opencode", "pi", "claude", "amp")
CLI_ALIASES = {
    "claude-code": "claude",
    "claudecode": "claude",
}
CLI_BINARIES = {
    "codex": "codex",
    "kimi": "kimi",
    "opencode": "opencode",
    "pi": "pi",
    "claude": "claude",
    "amp": "amp",
}
NPM_CLI_PACKAGES = {
    "codex": "@openai/codex",
    "opencode": "opencode-ai",
    "pi": "@mariozechner/pi-coding-agent",
    "claude": "@anthropic-ai/claude-code",
    "amp": "@sourcegraph/amp",
}
POLL_SECONDS = 5
POD_SSH_TIMEOUT_SECONDS = 1800
SSH_TRANSPORT_TIMEOUT_SECONDS = 240
PROJECT_EXCLUDES = (
    "node_modules/",
    "node_modules/**",
    ".venv/",
    ".venv/**",
    "venv/",
    "venv/**",
    "env/",
    "env/**",
    "__pycache__/",
    "__pycache__/**",
    ".pytest_cache/",
    ".pytest_cache/**",
    ".mypy_cache/",
    ".mypy_cache/**",
    ".ruff_cache/",
    ".ruff_cache/**",
    "dist/",
    "dist/**",
    "build/",
    "build/**",
)
CLI_SYNC_RULES: dict[str, dict[str, Any]] = {
    "codex": {
        "dirs": [
            {
                "local_rel": ".codex",
                "remote_rel": ".codex",
                "missing": "Codex state not found at ~/.codex; skipping Codex sync.",
                "auth_excludes": ["auth.json"],
            }
        ]
    },
    "kimi": {
        "dirs": [
            {
                "local_rel": ".kimi",
                "remote_rel": ".kimi",
                "missing": "Kimi state not found at ~/.kimi; skipping Kimi sync.",
                # Keep kimi.json even with --skip-auth so working-directory ->
                # last_session_id mappings survive and resume keeps working.
                "auth_excludes": ["config.toml", "device_id"],
            }
        ]
    },
    "opencode": {
        "dirs": [
            {
                "local_rel": ".config/opencode",
                "remote_rel": ".config/opencode",
                "missing": "OpenCode config not found at ~/.config/opencode; skipping config sync.",
            },
            {
                "local_rel": ".local/share/opencode",
                "remote_rel": ".local/share/opencode",
                "missing": "OpenCode data not found at ~/.local/share/opencode; skipping data sync.",
                "auth_excludes": ["auth.json"],
            },
        ]
    },
    "pi": {
        "dirs": [
            {
                "local_rel": ".pi",
                "remote_rel": ".pi",
                "auth_excludes": ["agent/auth.json"],
            },
            {
                "local_rel": ".config/pi",
                "remote_rel": ".config/pi",
            },
            {
                "local_rel": ".local/share/pi",
                "remote_rel": ".local/share/pi",
            },
        ],
        "missing_if_none": "Pi state not found in ~/.pi, ~/.config/pi, or ~/.local/share/pi; skipping Pi sync.",
    },
    "claude": {
        "dirs": [
            {
                "local_rel": ".claude",
                "remote_rel": ".claude",
                "missing": "Claude state not found at ~/.claude; skipping Claude state directory sync.",
                "auth_excludes": [".encryption_key"],
            }
        ],
        "files": [
            {
                "local_rel": ".claude.json",
                "remote_rel": ".claude.json",
                "skip_auth": True,
            }
        ],
    },
    "amp": {
        "dirs": [
            {
                "local_rel": ".amp",
                "remote_rel": ".amp",
                "missing": "Amp state not found at ~/.amp; skipping Amp state directory sync.",
            },
            {
                "local_rel": ".config/amp",
                "remote_rel": ".config/amp",
                "missing": "Amp config not found at ~/.config/amp; skipping Amp config sync.",
            },
        ]
    },
}


@dataclass(frozen=True)
class SSHConn:
    user: str
    host: str
    port: int
    key_path: Path


@dataclass(frozen=True)
class PodSummary:
    pod_id: str
    pod_name: str
    resource_id: str
    provider: str
    location: str
    price: str
    conn: SSHConn
    remote_project_dir: str
    copied_clis: tuple[str, ...]


def log(message: str) -> None:
    print(f"[prime-handoff] {message}", file=sys.stderr)


def run_cmd(
    cmd: Sequence[str],
    *,
    capture: bool = True,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(cmd),
            check=check,
            capture_output=capture,
            text=text,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {cmd[0]!r}") from exc
    except subprocess.CalledProcessError as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        raise RuntimeError(
            "Command failed.\n"
            f"cmd: {shlex.join(cmd)}\n"
            f"exit: {exc.returncode}\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}"
        ) from exc


def shell_quote_remote(path: str) -> str:
    return "'" + path.replace("'", "'\"'\"'") + "'"


def parse_clis(raw: str) -> list[str]:
    parsed_raw: list[str] = []
    for part in raw.split(","):
        value = part.strip().lower()
        if value:
            parsed_raw.append(value)
    parsed = [CLI_ALIASES.get(cli, cli) for cli in parsed_raw]
    unsupported = [cli for cli in parsed if cli not in SUPPORTED_CLIS]
    if unsupported:
        supported = ", ".join(SUPPORTED_CLIS)
        raise RuntimeError(
            f"Unsupported value(s) in --clis: {', '.join(unsupported)}. "
            f"Supported values: {supported}"
        )
    # Keep order, remove duplicates.
    deduped: list[str] = []
    for cli in parsed:
        if cli not in deduped:
            deduped.append(cli)
    return deduped


def sanitize_pod_name(prefix: str, kind: str, index: int) -> str:
    timestamp = time.strftime("%y%m%d-%H%M%S")
    raw = f"{prefix}-{kind}-{index}-{timestamp}"
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "-", raw).strip("-")
    if not cleaned:
        cleaned = f"handoff-{kind}-{index}"
    if not any(ch.isalpha() for ch in cleaned):
        cleaned = f"handoff-{cleaned}"
    return cleaned[:48]


def read_prime_config() -> dict[str, Any]:
    config_path = Path.home() / ".prime" / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {config_path}") from exc


def resolve_ssh_key_path() -> Path:
    config = read_prime_config()
    config_key = config.get("ssh_key_path")
    config_value = config_key.strip() if isinstance(config_key, str) else None

    selected = (
        os.getenv("PRIME_SSH_KEY_PATH")
        or config_value
        or str(Path.home() / ".ssh" / "id_rsa")
    )
    key_path = Path(selected).expanduser()

    # Prime config sometimes points to .pub; rsync/ssh need the private key.
    if str(key_path).endswith(".pub"):
        key_path = Path(str(key_path)[: -len(".pub")])

    if not key_path.exists():
        raise RuntimeError(
            f"Resolved SSH private key does not exist: {key_path}. "
            "Configure with `prime config set-ssh-key-path` or PRIME_SSH_KEY_PATH."
        )
    return key_path


def prime_availability(
    *,
    gpu_type: str,
    gpu_count: int | None,
    regions: list[str],
    socket: str | None,
) -> list[dict[str, Any]]:
    base_cmd = [
        "prime",
        "availability",
        "list",
        "--output",
        "json",
        "--no-group-similar",
    ]
    cmd = list(base_cmd)
    if gpu_type:
        cmd += ["--gpu-type", gpu_type]
    if gpu_count is not None:
        cmd += ["--gpu-count", str(gpu_count)]
    if regions:
        cmd += ["--regions", ",".join(regions)]
    if socket:
        cmd += ["--socket", socket]

    output = run_cmd(cmd).stdout
    payload = json.loads(output)
    resources = payload.get("gpu_resources")
    if not isinstance(resources, list):
        raise RuntimeError("Unexpected JSON from `prime availability list`.")
    return resources


def availability_candidates(
    resources: list[dict[str, Any]], spot_mode: str
) -> list[dict[str, Any]]:
    def looks_in_stock(entry: dict[str, Any]) -> bool:
        value = str(entry.get("stock_status", "")).lower()
        return not any(bad in value for bad in ("out", "unavailable", "none", "error"))

    def is_spot(entry: dict[str, Any]) -> bool:
        if bool(entry.get("is_spot")):
            return True
        return "spot" in str(entry.get("gpu_type", "")).lower()

    in_stock = [entry for entry in resources if looks_in_stock(entry)]
    if in_stock:
        resources = in_stock

    if spot_mode == "only":
        return [entry for entry in resources if is_spot(entry)]
    if spot_mode == "avoid":
        non_spot = [entry for entry in resources if not is_spot(entry)]
        return non_spot if non_spot else resources
    return resources


def parse_resource_int(entry: dict[str, Any], field: str) -> int:
    value = entry.get(field)
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"\d+", str(value))
    if match:
        return int(match.group(0))
    resource_id = entry.get("id", "<unknown>")
    raise RuntimeError(
        f"Availability field {field!r} for resource {resource_id!r} "
        f"does not contain a number: {value!r}"
    )


def create_pod(
    *,
    resource_id: str,
    pod_name: str,
    disk_size: int,
    vcpus: int,
    memory_gb: int,
    image: str,
    team_id: str | None,
) -> str:
    cmd = [
        "prime",
        "pods",
        "create",
        "--id",
        resource_id,
        "--name",
        pod_name,
        "--disk-size",
        str(disk_size),
        "--vcpus",
        str(vcpus),
        "--memory",
        str(memory_gb),
        "--image",
        image,
        "--yes",
    ]
    if team_id:
        cmd += ["--team-id", team_id]

    output = run_cmd(cmd).stdout

    success_match = re.search(r"Successfully created pod\s+([0-9a-fA-F]{32,})", output)
    if success_match:
        return success_match.group(1)
    raise RuntimeError("Could not parse pod id from `prime pods create` output.")


def pod_status(pod_id: str) -> dict[str, Any]:
    output = run_cmd(["prime", "pods", "status", pod_id, "--output", "json"]).stdout
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected JSON from `prime pods status`.")
    return payload


def parse_ssh_from_status(status: dict[str, Any]) -> tuple[str, str, int] | None:
    raw_value = status.get("ssh")
    if raw_value in (None, "", "N/A"):
        return None
    text = str(raw_value).strip()
    match = re.fullmatch(
        r"(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.\-]+)(?:\s+-p\s+(?P<port>\d+))?",
        text,
    )
    if not match:
        return None
    user = match.group("user")
    host = match.group("host")
    port = int(match.group("port") or "22")
    return user, host, port


def wait_for_pod_ssh(
    pod_id: str,
    *,
    poll_seconds: int,
    timeout_seconds: int,
) -> tuple[dict[str, Any], tuple[str, str, int]]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = pod_status(pod_id)
        ssh_tuple = parse_ssh_from_status(status)
        if ssh_tuple:
            return status, ssh_tuple
        time.sleep(poll_seconds)
    raise TimeoutError(f"Timed out waiting for SSH details on pod {pod_id}.")


def ssh_base_args(conn: SSHConn) -> list[str]:
    return [
        "ssh",
        "-i",
        str(conn.key_path),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(conn.port),
        f"{conn.user}@{conn.host}",
    ]


def ssh_run(
    conn: SSHConn, command: str, *, capture: bool = True, check: bool = True
) -> subprocess.CompletedProcess[str]:
    remote = f"bash -lc {shlex.quote(command)}"
    return run_cmd(ssh_base_args(conn) + [remote], capture=capture, check=check)


def wait_for_ssh_transport(
    conn: SSHConn,
    *,
    poll_seconds: int,
    timeout_seconds: int,
) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        probe = subprocess.run(
            ssh_base_args(conn) + ["true"], capture_output=True, text=True
        )
        if probe.returncode == 0:
            return
        time.sleep(poll_seconds)
    raise TimeoutError(
        f"Timed out waiting for SSH transport to become reachable: {conn.host}:{conn.port}"
    )


def remote_home(conn: SSHConn) -> str:
    output = ssh_run(conn, 'printf %s "$HOME"').stdout.strip()
    if not output:
        raise RuntimeError("Remote $HOME is empty.")
    return output


def ensure_remote_dir(conn: SSHConn, remote_dir: str) -> None:
    command = f"mkdir -p {shlex.quote(remote_dir)}"
    ssh_run(conn, command)


def run_remote_step(
    conn: SSHConn,
    *,
    description: str,
    command: str,
) -> None:
    log(description)
    ssh_run(conn, command, capture=False)


def remote_apt_install_command(packages: Sequence[str]) -> str:
    package_str = " ".join(shlex.quote(package) for package in packages)
    return f"""
if ! command -v apt-get >/dev/null 2>&1; then
  echo "Automatic package install requires apt-get: {package_str}" >&2
  exit 1
fi
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  echo "Need root or sudo to install: {package_str}" >&2
  exit 1
fi
run_root() {{
  if [ -n "$SUDO" ]; then
    $SUDO "$@"
  else
    "$@"
  fi
}}
apt_retry() {{
  local tries=0
  local max_tries=40
  while true; do
    if run_root "$@"; then
      return 0
    fi
    tries=$((tries + 1))
    if [ "$tries" -ge "$max_tries" ]; then
      echo "Command failed after ${{max_tries}} attempts: $*" >&2
      return 1
    fi
    sleep 3
  done
}}
apt_retry apt-get update -y
apt_retry env DEBIAN_FRONTEND=noninteractive apt-get install -y {package_str}
""".strip()


def ensure_remote_nodejs(conn: SSHConn) -> None:
    command = f"""
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  if [ "${{node_major}}" -ge 18 ]; then
    exit 0
  fi
fi
{remote_apt_install_command(["curl", "ca-certificates", "nodejs", "npm"])}
node_major="$(node -p 'process.versions.node.split(".")[0]')"
if [ "${{node_major}}" -lt 18 ]; then
  run_root npm install -g n
  run_root n 20
  export PATH="/usr/local/bin:$PATH"
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  if [ "${{node_major}}" -lt 18 ]; then
    echo "Node.js >=18 is required." >&2
    exit 1
  fi
fi
""".strip()
    run_remote_step(
        conn,
        description="Installing Node.js/npm on remote pod (required for selected CLI installs).",
        command=command,
    )


def ensure_remote_tmux(conn: SSHConn) -> None:
    command = f"""
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
if command -v tmux >/dev/null 2>&1; then
  exit 0
fi
{remote_apt_install_command(["tmux"])}
if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux install completed but tmux command is not on PATH." >&2
  exit 1
fi
""".strip()
    run_remote_step(
        conn,
        description="Installing tmux on remote pod.",
        command=command,
    )


def ensure_remote_uv(conn: SSHConn) -> None:
    command = f"""
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
if command -v uv >/dev/null 2>&1; then
  exit 0
fi
if ! command -v curl >/dev/null 2>&1; then
{remote_apt_install_command(["curl", "ca-certificates"])}
fi
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv install completed but uv command is not on PATH." >&2
  exit 1
fi
""".strip()
    run_remote_step(
        conn,
        description="Installing uv on remote pod.",
        command=command,
    )


def install_remote_clis(
    conn: SSHConn,
    *,
    clis: list[str],
) -> None:
    if not clis:
        return

    missing: list[str] = []
    for cli in clis:
        probe = ssh_run(
            conn,
            (
                'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"; '
                f"command -v {shlex.quote(CLI_BINARIES[cli])} >/dev/null 2>&1"
            ),
            check=False,
        )
        if probe.returncode != 0:
            missing.append(cli)

    if not missing:
        log("All selected CLIs already exist on remote pod; skipping installation.")
        return

    log(f"Remote CLI install check: missing {', '.join(missing)}")
    if any(cli in NPM_CLI_PACKAGES for cli in missing):
        ensure_remote_nodejs(conn)

    for cli in missing:
        if cli == "kimi":
            kimi_command = f"""
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
if command -v kimi >/dev/null 2>&1; then
  exit 0
fi
if ! command -v curl >/dev/null 2>&1; then
{remote_apt_install_command(["curl", "ca-certificates"])}
fi
curl -LsSf https://code.kimi.com/install.sh | bash
""".strip()
            run_remote_step(
                conn,
                description="Installing Kimi CLI on remote pod via official installer.",
                command=kimi_command,
            )
            continue

        package_name = NPM_CLI_PACKAGES[cli]
        binary_name = CLI_BINARIES[cli]
        npm_command = f"""
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
if command -v {shlex.quote(binary_name)} >/dev/null 2>&1; then
  exit 0
fi
if [ "$(id -u)" -eq 0 ]; then
  npm install -g {shlex.quote(package_name)}
elif command -v sudo >/dev/null 2>&1; then
  sudo npm install -g {shlex.quote(package_name)}
else
  npm install -g --prefix "$HOME/.local" {shlex.quote(package_name)}
fi
""".strip()
        run_remote_step(
            conn,
            description=f"Installing {cli} on remote pod via npm package {package_name}.",
            command=npm_command,
        )


def rsync_ssh_arg(conn: SSHConn) -> str:
    parts = [
        "ssh",
        "-i",
        str(conn.key_path),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(conn.port),
    ]
    return " ".join(shlex.quote(part) for part in parts)


def rsync_transfer(
    conn: SSHConn,
    *,
    src: Path,
    remote_path: str,
    excludes: Sequence[str],
    as_dir: bool,
) -> None:
    source = f"{str(src)}/" if as_dir else str(src)
    target_path = remote_path.rstrip("/") + "/" if as_dir else remote_path
    remote_target = f"{conn.user}@{conn.host}:{shell_quote_remote(target_path)}"
    cmd = ["rsync", "-az"]
    for pattern in excludes:
        cmd += ["--exclude", pattern]
    cmd += ["-e", rsync_ssh_arg(conn), source, remote_target]
    run_cmd(cmd, capture=False)


def sync_cli_state(
    conn: SSHConn,
    *,
    clis: list[str],
    remote_home_dir: str,
    skip_auth: bool,
) -> tuple[str, ...]:
    local_home = Path.home()
    remote_home = Path(remote_home_dir)
    copied: list[str] = []

    for cli in clis:
        rules = CLI_SYNC_RULES[cli]
        any_dir_synced = False

        for dir_rule in rules.get("dirs", []):
            local_dir = local_home / str(dir_rule["local_rel"])
            if not local_dir.exists():
                missing_message = dir_rule.get("missing")
                if missing_message:
                    log(str(missing_message))
                continue

            excludes = list(dir_rule.get("excludes", []))
            if skip_auth:
                excludes.extend(dir_rule.get("auth_excludes", []))

            remote_dir = remote_home / str(dir_rule["remote_rel"])
            ensure_remote_dir(conn, str(remote_dir))
            rsync_transfer(
                conn,
                src=local_dir,
                remote_path=str(remote_dir),
                excludes=excludes,
                as_dir=True,
            )
            any_dir_synced = True

        if not any_dir_synced and rules.get("missing_if_none"):
            log(str(rules["missing_if_none"]))

        for file_rule in rules.get("files", []):
            if skip_auth and bool(file_rule.get("skip_auth")):
                continue
            local_file = local_home / str(file_rule["local_rel"])
            if not local_file.exists():
                continue

            remote_file = remote_home / str(file_rule["remote_rel"])
            ensure_remote_dir(conn, str(remote_file.parent))
            rsync_transfer(
                conn,
                src=local_file,
                remote_path=str(remote_file),
                excludes=[],
                as_dir=False,
            )

        copied.append(cli)

    return tuple(copied)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="prime_handoff.py",
        formatter_class=lambda prog: argparse.RawTextHelpFormatter(
            prog, max_help_position=42, width=110
        ),
        description=(
            "Create Prime pod(s), wait for SSH, sync current project, and sync coding-agent state.\n\n"
            "This is designed for agent handoff: start on local machine, then SSH into the new node and keep working.\n"
            "Selected CLIs are installed automatically on the remote node when missing."
        ),
        epilog=(
            "Examples:\n"
            "  uv run prime_handoff.py --kind cpu --clis codex --node-count 1\n"
            "  uv run prime_handoff.py --kind gpu --gpu-type H100_80GB --gpu-count 1 --clis codex,kimi\n"
            "  uv run prime_handoff.py --kind cpu --node-count 1 --skip-project --clis codex\n"
            "  uv run prime_handoff.py --kind cpu --node-count 2 --skip-auth --clis codex\n\n"
            "Security note:\n"
            "  By default, auth files are copied. Use --skip-auth if you want to login again on the remote side."
        ),
    )

    parser.add_argument(
        "--kind",
        choices=("cpu", "gpu"),
        default="cpu",
        help=(
            "What kind of resource to create.\n"
            "cpu: uses --cpu-type (default CPU_NODE)\n"
            "gpu: uses --gpu-type/--gpu-count (default H100_80GB x1)"
        ),
    )
    parser.add_argument(
        "--node-count",
        type=int,
        default=1,
        help=(
            "How many pods to create in this run.\n"
            "Each pod receives the same sync payload (project + selected CLI state)."
        ),
    )
    parser.add_argument(
        "--cpu-type",
        default="CPU_NODE",
        help=(
            "Prime availability filter for CPU runs.\n"
            "Examples: CPU_NODE (on-demand), CPU_NODE_SPOT-like labels depending on provider naming."
        ),
    )
    parser.add_argument(
        "--gpu-type",
        default="H100_80GB",
        help=(
            "Prime availability filter for GPU runs.\n"
            "Default requests H100 80GB resources."
        ),
    )
    parser.add_argument(
        "--gpu-count",
        type=int,
        default=1,
        help=("Number of GPUs required when --kind gpu.\nIgnored for --kind cpu."),
    )
    parser.add_argument(
        "--spot",
        choices=("avoid", "allow", "only"),
        default="avoid",
        help=(
            "Spot preference during candidate selection.\n"
            "avoid: prefer non-spot\n"
            "allow: allow both non-spot and spot\n"
            "only: only spot"
        ),
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="Optional socket filter passed to `prime availability list` (example: PCIe, SXM5).",
    )
    parser.add_argument(
        "--region",
        action="append",
        default=[],
        help=(
            "Optional region filter. Repeat flag for multiple values.\n"
            "Example: --region united_states --region germany"
        ),
    )
    parser.add_argument(
        "--image",
        default="ubuntu_22_cuda_12",
        help=(
            "Pod image for `prime pods create`.\n"
            "Must be one of the images accepted by Prime for the selected resource."
        ),
    )
    parser.add_argument(
        "--disk-size",
        type=int,
        default=None,
        help=(
            "Disk size in GB.\n"
            "If omitted, script uses the selected availability entry value."
        ),
    )
    parser.add_argument(
        "--vcpus",
        type=int,
        default=None,
        help="Override vCPU count. If omitted, script uses availability defaults.",
    )
    parser.add_argument(
        "--memory",
        type=int,
        default=None,
        help="Override memory in GB. If omitted, script uses availability defaults.",
    )
    parser.add_argument(
        "--team-id",
        default=None,
        help=(
            "Optional team id for pod creation.\n"
            "If omitted, Prime CLI current context/team config is used."
        ),
    )
    parser.add_argument(
        "--name-prefix",
        default=None,
        help=(
            "Optional pod name prefix. Default is current directory name.\n"
            "Script appends kind/index/timestamp automatically."
        ),
    )
    parser.add_argument(
        "--skip-project",
        action="store_true",
        help=(
            "Do not copy the current working directory. Agent state can still be synced.\n"
            "Most handoff flows should keep this disabled."
        ),
    )

    parser.add_argument(
        "--clis",
        default="codex,kimi,opencode,pi,claude,amp",
        help=(
            "Comma-separated list of coding CLIs whose state should be copied.\n"
            "Supported values: codex,kimi,opencode,pi,claude,amp\n"
            "Alias: claude-code -> claude\n"
            "Missing selected CLIs are installed automatically on the remote node."
        ),
    )
    parser.add_argument(
        "--skip-auth",
        action="store_true",
        help=(
            "Skip files that likely contain credentials/tokens.\n"
            "Use this when you prefer to login again on the remote node."
        ),
    )

    args = parser.parse_args(argv)
    if args.node_count < 1:
        parser.error("--node-count must be >= 1")
    if args.gpu_count < 1:
        parser.error("--gpu-count must be >= 1")
    return args


def create_single_pod(
    *,
    args: argparse.Namespace,
    cwd: Path,
    key_path: Path,
    pod_index: int,
) -> PodSummary:
    gpu_type = args.cpu_type if args.kind == "cpu" else args.gpu_type
    gpu_count = None if args.kind == "cpu" else args.gpu_count

    resources = prime_availability(
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        regions=args.region,
        socket=args.socket,
    )
    if not resources:
        raise RuntimeError(
            f"No availability entries for gpu_type={gpu_type!r} gpu_count={gpu_count!r} "
            f"with current filters."
        )
    candidates = availability_candidates(resources, args.spot)
    if not candidates:
        raise RuntimeError(
            "No availability candidates left after spot/in-stock filtering."
        )
    candidates = sorted(candidates, key=lambda entry: str(entry.get("id", "")))

    prefix = args.name_prefix or cwd.name or "handoff"
    pod_name = sanitize_pod_name(prefix, args.kind, pod_index)
    chosen_entry = candidates[0]
    resource_id = str(chosen_entry.get("id", "")).strip()
    if not resource_id:
        raise RuntimeError("Selected availability entry has no resource id.")

    disk_size = (
        args.disk_size
        if args.disk_size is not None
        else parse_resource_int(chosen_entry, "disk_gb")
    )
    vcpus = (
        args.vcpus
        if args.vcpus is not None
        else parse_resource_int(chosen_entry, "vcpus")
    )
    memory = (
        args.memory
        if args.memory is not None
        else parse_resource_int(chosen_entry, "memory_gb")
    )

    pod_id = create_pod(
        resource_id=resource_id,
        pod_name=pod_name,
        disk_size=disk_size,
        vcpus=vcpus,
        memory_gb=memory,
        image=args.image,
        team_id=args.team_id,
    )

    log(f"Pod created: {pod_id} (name={pod_name})")
    _, (ssh_user, ssh_host, ssh_port) = wait_for_pod_ssh(
        pod_id,
        poll_seconds=POLL_SECONDS,
        timeout_seconds=POD_SSH_TIMEOUT_SECONDS,
    )

    conn = SSHConn(user=ssh_user, host=ssh_host, port=ssh_port, key_path=key_path)
    wait_for_ssh_transport(
        conn,
        poll_seconds=POLL_SECONDS,
        timeout_seconds=SSH_TRANSPORT_TIMEOUT_SECONDS,
    )
    remote_home_dir = remote_home(conn)
    remote_project_dir = str(cwd)
    clis = parse_clis(args.clis)

    ensure_remote_tmux(conn)
    ensure_remote_uv(conn)
    install_remote_clis(conn, clis=clis)

    if not args.skip_project:
        log(f"Syncing project {cwd} -> {remote_project_dir}")
        ensure_remote_dir(conn, remote_project_dir)
        rsync_transfer(
            conn,
            src=cwd,
            remote_path=remote_project_dir,
            excludes=PROJECT_EXCLUDES,
            as_dir=True,
        )
    else:
        log("Skipping project sync because --skip-project is set.")

    copied_clis = sync_cli_state(
        conn,
        clis=clis,
        remote_home_dir=remote_home_dir,
        skip_auth=args.skip_auth,
    )

    return PodSummary(
        pod_id=pod_id,
        pod_name=pod_name,
        resource_id=str(chosen_entry.get("id", "")),
        provider=str(chosen_entry.get("provider", "")),
        location=str(chosen_entry.get("location", "")),
        price=str(chosen_entry.get("price_per_hour", "")),
        conn=conn,
        remote_project_dir=remote_project_dir,
        copied_clis=copied_clis,
    )


def print_summary(summaries: list[PodSummary]) -> None:
    print("\n=== Prime Handoff Complete ===")
    for idx, item in enumerate(summaries, start=1):
        print(f"\nPod {idx}")
        print(f"  pod_id: {item.pod_id}")
        print(f"  pod_name: {item.pod_name}")
        print(f"  resource_id: {item.resource_id}")
        print(f"  provider: {item.provider}")
        print(f"  location: {item.location}")
        print(f"  price_per_hour: {item.price}")
        print(
            f"  ssh: ssh -i {item.conn.key_path} -p {item.conn.port} {item.conn.user}@{item.conn.host}"
        )
        print(f"  remote_project_dir: {item.remote_project_dir}")
        print(
            f"  copied_clis: {', '.join(item.copied_clis) if item.copied_clis else '(none)'}"
        )
    print("\nTerminate later with: prime pods terminate <pod_id>")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run_cmd(["prime", "--version"])
    cwd = Path.cwd().resolve()
    key_path = resolve_ssh_key_path()

    summaries: list[PodSummary] = []
    for index in range(1, args.node_count + 1):
        log(f"Creating pod {index}/{args.node_count} ...")
        summary = create_single_pod(
            args=args, cwd=cwd, key_path=key_path, pod_index=index
        )
        summaries.append(summary)

    print_summary(summaries)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
