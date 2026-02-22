#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Provision Prime pod(s) and hand off local coding-agent state.

This script creates one or more Prime Intellect pods (CPU or GPU), waits for SSH,
copies the current working directory, and syncs local CLI state for supported
coding agents (Codex, Kimi, OpenCode, Pi, Claude Code, Amp).

It is intentionally explicit and verbose in its CLI docs so another agent can
use it safely without digging into source code.
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
TERMINAL_POD_STATUSES = {"ERROR", "FAILED", "TERMINATED", "DELETING"}
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
MANDATORY_PROJECT_EXCLUDES = (
    ".prime-handoff/transcripts/",
    ".prime-handoff/transcripts/**",
)


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


def parse_first_int(value: Any, fallback: int) -> int:
    match = re.search(r"\d+", str(value))
    if not match:
        return fallback
    return int(match.group(0))


def shell_quote_remote(path: str) -> str:
    return "'" + path.replace("'", "'\"'\"'") + "'"


def parse_csv_list(raw: str, *, lower: bool = True) -> list[str]:
    values = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        values.append(value.lower() if lower else value)
    return values


def parse_clis(raw: str) -> list[str]:
    parsed_raw = parse_csv_list(raw)
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
    except Exception:
        return {}


def resolve_ssh_key_path() -> Path:
    env_override = os.getenv("PRIME_SSH_KEY_PATH")
    candidates: list[Path] = []
    if env_override:
        candidates.append(Path(env_override).expanduser())

    config = read_prime_config()
    config_key = config.get("ssh_key_path")
    if isinstance(config_key, str) and config_key.strip():
        candidates.append(Path(config_key).expanduser())

    if not candidates:
        candidates.append(Path.home() / ".ssh" / "id_rsa")

    for candidate in candidates:
        # Prime config sometimes points to .pub; rsync/ssh need the private key.
        if str(candidate).endswith(".pub"):
            private_candidate = Path(str(candidate)[: -len(".pub")])
            if private_candidate.exists():
                return private_candidate
            if candidate.exists():
                raise RuntimeError(
                    f"SSH key path {candidate} is a public key and matching private key "
                    f"{private_candidate} was not found."
                )
            continue
        if candidate.exists():
            return candidate

    checked = ", ".join(str(path) for path in candidates)
    raise RuntimeError(
        "Could not resolve a usable SSH private key. "
        f"Checked: {checked}. Configure with `prime config set-ssh-key-path`."
    )


def prime_availability(
    *,
    gpu_type: str,
    gpu_count: int | None,
    regions: list[str],
    provider: str | None,
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
    if provider:
        cmd += ["--provider", provider]
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
        for bad in ("out", "unavailable", "none", "error"):
            if bad in value:
                return False
        return True

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


def create_pod(
    *,
    resource_id: str,
    pod_name: str,
    disk_size: int,
    vcpus: int,
    memory_gb: int,
    image: str,
    team_id: str | None,
    dry_run: bool,
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

    if dry_run:
        log(f"[dry-run] {shlex.join(cmd)}")
        return f"dry-run-{pod_name}"

    output = run_cmd(cmd).stdout

    success_match = re.search(r"Successfully created pod\s+([0-9a-fA-F]{32,})", output)
    if success_match:
        return success_match.group(1)

    fallback_match = re.search(r"\b([0-9a-fA-F]{32})\b", output)
    if fallback_match:
        return fallback_match.group(1)

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
    text = str(raw_value)
    match = re.search(
        r"(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.\-]+)(?:\s+-p\s+(?P<port>\d+))?",
        text,
    )
    if not match:
        return None
    user = match.group("user")
    host = match.group("host")
    port = int(match.group("port") or 22)
    return user, host, port


def wait_for_pod_ssh(
    pod_id: str,
    *,
    poll_seconds: int,
    timeout_seconds: int,
    dry_run: bool,
) -> tuple[dict[str, Any], tuple[str, str, int]]:
    if dry_run:
        fake_status = {"id": pod_id, "status": "ACTIVE", "ssh": "root@127.0.0.1 -p 22"}
        return fake_status, ("root", "127.0.0.1", 22)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = pod_status(pod_id)
        ssh_tuple = parse_ssh_from_status(status)
        if ssh_tuple:
            return status, ssh_tuple
        state = str(status.get("status", "")).upper()
        if state in TERMINAL_POD_STATUSES:
            raise RuntimeError(f"Pod entered terminal state {state}: {status}")
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
    dry_run: bool,
) -> None:
    if dry_run:
        return
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


def remote_home(conn: SSHConn, *, dry_run: bool) -> str:
    if dry_run:
        return "/root"
    output = ssh_run(conn, 'printf %s "$HOME"').stdout.strip()
    if output:
        return output
    return "/root" if conn.user == "root" else f"/home/{conn.user}"


def ensure_remote_dir(conn: SSHConn, remote_dir: str, *, dry_run: bool) -> None:
    command = f"mkdir -p {shlex.quote(remote_dir)}"
    cmd = ssh_base_args(conn) + [f"bash -lc {shlex.quote(command)}"]
    if dry_run:
        log(f"[dry-run] {shlex.join(cmd)}")
        return
    ssh_run(conn, command)


def remote_command_exists(conn: SSHConn, command: str, *, dry_run: bool) -> bool:
    if dry_run:
        return False
    probe = ssh_run(
        conn,
        f'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"; command -v {shlex.quote(command)} >/dev/null 2>&1',
        check=False,
    )
    return probe.returncode == 0


def run_remote_step(
    conn: SSHConn,
    *,
    description: str,
    command: str,
    dry_run: bool,
) -> None:
    cmd = ssh_base_args(conn) + [f"bash -lc {shlex.quote(command)}"]
    if dry_run:
        log(f"[dry-run] {description}")
        log(f"[dry-run] {shlex.join(cmd)}")
        return
    log(description)
    ssh_run(conn, command, capture=False)


def ensure_remote_nodejs(conn: SSHConn, *, dry_run: bool) -> None:
    command = """
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
node_ok() {
  if ! command -v node >/dev/null 2>&1; then
    return 1
  fi
  if ! command -v npm >/dev/null 2>&1; then
    return 1
  fi
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  [ "${node_major}" -ge 18 ]
}
if node_ok; then
  exit 0
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo "Automatic Node.js install currently supports apt-based images only." >&2
  exit 1
fi
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  echo "Need root or sudo to install Node.js." >&2
  exit 1
fi
run_root() {
  if [ -n "$SUDO" ]; then
    $SUDO "$@"
  else
    "$@"
  fi
}
apt_retry() {
  local tries=0
  local max_tries=60
  while true; do
    if run_root env DEBIAN_FRONTEND=noninteractive apt-get "$@"; then
      return 0
    fi
    tries=$((tries + 1))
    if [ "$tries" -ge "$max_tries" ]; then
      echo "apt-get failed after ${max_tries} attempts: $*" >&2
      return 1
    fi
    sleep 3
  done
}
apt_retry update -y
apt_retry install -y curl ca-certificates nodejs npm
if node_ok; then
  exit 0
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "npm was not installed successfully." >&2
  exit 1
fi
if [ -n "$SUDO" ]; then
  $SUDO npm install -g n
  $SUDO n 20
else
  npm install -g n
  n 20
fi
export PATH="/usr/local/bin:$PATH"
if ! node_ok; then
  echo "Failed to provision a usable Node.js/npm toolchain (need node>=18)." >&2
  exit 1
fi
""".strip()
    run_remote_step(
        conn,
        description="Installing Node.js/npm on remote pod (required for selected CLI installs).",
        command=command,
        dry_run=dry_run,
    )


def ensure_remote_tmux(conn: SSHConn, *, dry_run: bool) -> None:
    command = """
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
if command -v tmux >/dev/null 2>&1; then
  exit 0
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo "Automatic tmux install currently supports apt-based images only." >&2
  exit 1
fi
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  echo "Need root or sudo to install tmux." >&2
  exit 1
fi
run_root() {
  if [ -n "$SUDO" ]; then
    $SUDO "$@"
  else
    "$@"
  fi
}
apt_retry() {
  local tries=0
  local max_tries=60
  while true; do
    if run_root env DEBIAN_FRONTEND=noninteractive apt-get "$@"; then
      return 0
    fi
    tries=$((tries + 1))
    if [ "$tries" -ge "$max_tries" ]; then
      echo "apt-get failed after ${max_tries} attempts: $*" >&2
      return 1
    fi
    sleep 3
  done
}
apt_retry update -y
apt_retry install -y tmux
if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux install completed but tmux command is not on PATH." >&2
  exit 1
fi
""".strip()
    run_remote_step(
        conn,
        description="Installing tmux on remote pod.",
        command=command,
        dry_run=dry_run,
    )


def install_remote_npm_cli(
    conn: SSHConn,
    *,
    cli_name: str,
    binary_name: str,
    package_name: str,
    dry_run: bool,
) -> None:
    command = f"""
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
        description=f"Installing {cli_name} on remote pod via npm package {package_name}.",
        command=command,
        dry_run=dry_run,
    )


def install_remote_kimi(conn: SSHConn, *, dry_run: bool) -> None:
    command = """
set -euo pipefail
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
if command -v kimi >/dev/null 2>&1; then
  exit 0
fi
if ! command -v curl >/dev/null 2>&1; then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "Automatic Kimi install currently supports apt-based images only." >&2
    exit 1
  fi
  if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
  elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    echo "Need root or sudo to install curl before Kimi install." >&2
    exit 1
  fi
  if [ -n "$SUDO" ]; then
    apt_retry() {
      local tries=0
      local max_tries=60
      while true; do
        if $SUDO env DEBIAN_FRONTEND=noninteractive apt-get "$@"; then
          return 0
        fi
        tries=$((tries + 1))
        if [ "$tries" -ge "$max_tries" ]; then
          echo "apt-get failed after ${max_tries} attempts: $*" >&2
          return 1
        fi
        sleep 3
      done
    }
    apt_retry update -y
    apt_retry install -y curl ca-certificates
  else
    apt_retry() {
      local tries=0
      local max_tries=60
      while true; do
        if env DEBIAN_FRONTEND=noninteractive apt-get "$@"; then
          return 0
        fi
        tries=$((tries + 1))
        if [ "$tries" -ge "$max_tries" ]; then
          echo "apt-get failed after ${max_tries} attempts: $*" >&2
          return 1
        fi
        sleep 3
      done
    }
    apt_retry update -y
    apt_retry install -y curl ca-certificates
  fi
fi
curl -LsSf https://code.kimi.com/install.sh | bash
""".strip()
    run_remote_step(
        conn,
        description="Installing Kimi CLI on remote pod via official installer.",
        command=command,
        dry_run=dry_run,
    )


def install_remote_clis(
    conn: SSHConn,
    *,
    clis: list[str],
    dry_run: bool,
) -> None:
    if not clis:
        return

    if dry_run:
        missing = list(clis)
    else:
        missing = [
            cli
            for cli in clis
            if not remote_command_exists(conn, CLI_BINARIES[cli], dry_run=False)
        ]

    if not missing:
        log("All selected CLIs already exist on remote pod; skipping installation.")
        return

    log(f"Remote CLI install check: missing {', '.join(missing)}")
    if any(cli in NPM_CLI_PACKAGES for cli in missing):
        ensure_remote_nodejs(conn, dry_run=dry_run)

    for cli in missing:
        if cli == "kimi":
            install_remote_kimi(conn, dry_run=dry_run)
        else:
            package_name = NPM_CLI_PACKAGES[cli]
            install_remote_npm_cli(
                conn,
                cli_name=cli,
                binary_name=CLI_BINARIES[cli],
                package_name=package_name,
                dry_run=dry_run,
            )

        if not dry_run and not remote_command_exists(
            conn, CLI_BINARIES[cli], dry_run=False
        ):
            raise RuntimeError(
                f"Remote install for {cli!r} completed but command "
                f"{CLI_BINARIES[cli]!r} was still not found on PATH."
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


def rsync_dir(
    conn: SSHConn,
    *,
    src_dir: Path,
    remote_dir: str,
    excludes: list[str],
    dry_run: bool,
) -> None:
    remote_target = (
        f"{conn.user}@{conn.host}:{shell_quote_remote(remote_dir.rstrip('/') + '/')}"
    )
    cmd = ["rsync", "-az"]
    for pattern in excludes:
        cmd += ["--exclude", pattern]
    cmd += ["-e", rsync_ssh_arg(conn), f"{str(src_dir)}/", remote_target]
    if dry_run:
        log(f"[dry-run] {shlex.join(cmd)}")
        return
    run_cmd(cmd, capture=False)


def merged_project_excludes(user_patterns: Sequence[str]) -> list[str]:
    # Always exclude project-local handoff transcript artifacts. Those are not
    # native CLI resume paths and can interfere with resume behavior.
    merged: list[str] = []
    for pattern in list(user_patterns) + list(MANDATORY_PROJECT_EXCLUDES):
        if pattern not in merged:
            merged.append(pattern)
    return merged


def rsync_file(
    conn: SSHConn,
    *,
    src_file: Path,
    remote_path: str,
    dry_run: bool,
) -> None:
    remote_target = f"{conn.user}@{conn.host}:{shell_quote_remote(remote_path)}"
    cmd = ["rsync", "-az", "-e", rsync_ssh_arg(conn), str(src_file), remote_target]
    if dry_run:
        log(f"[dry-run] {shlex.join(cmd)}")
        return
    run_cmd(cmd, capture=False)


def try_copy_file(
    conn: SSHConn,
    *,
    local_file: Path,
    remote_file: str,
    dry_run: bool,
) -> bool:
    if not local_file.exists():
        return False
    ensure_remote_dir(conn, str(Path(remote_file).parent), dry_run=dry_run)
    rsync_file(conn, src_file=local_file, remote_path=remote_file, dry_run=dry_run)
    return True


def sync_codex_state(
    conn: SSHConn,
    *,
    remote_home_dir: str,
    skip_auth: bool,
    dry_run: bool,
) -> None:
    local_codex = Path.home() / ".codex"
    if not local_codex.exists():
        log("Codex state not found at ~/.codex; skipping Codex sync.")
        return

    remote_codex = Path(remote_home_dir) / ".codex"
    ensure_remote_dir(conn, str(remote_codex), dry_run=dry_run)
    excludes = ["auth.json"] if skip_auth else []
    rsync_dir(
        conn,
        src_dir=local_codex,
        remote_dir=str(remote_codex),
        excludes=excludes,
        dry_run=dry_run,
    )


def sync_kimi_state(
    conn: SSHConn,
    *,
    remote_home_dir: str,
    skip_auth: bool,
    dry_run: bool,
) -> None:
    local_kimi = Path.home() / ".kimi"
    if not local_kimi.exists():
        log("Kimi state not found at ~/.kimi; skipping Kimi sync.")
        return
    remote_kimi = Path(remote_home_dir) / ".kimi"
    # Keep kimi.json even with --skip-auth so working-directory -> last_session_id
    # mappings survive and resume keeps working.
    excludes = ["config.toml", "device_id"] if skip_auth else []
    ensure_remote_dir(conn, str(remote_kimi), dry_run=dry_run)
    rsync_dir(
        conn,
        src_dir=local_kimi,
        remote_dir=str(remote_kimi),
        excludes=excludes,
        dry_run=dry_run,
    )


def sync_opencode_state(
    conn: SSHConn,
    *,
    remote_home_dir: str,
    skip_auth: bool,
    dry_run: bool,
) -> None:
    local_config = Path.home() / ".config" / "opencode"
    local_data = Path.home() / ".local" / "share" / "opencode"

    if local_config.exists():
        remote_config = Path(remote_home_dir) / ".config" / "opencode"
        ensure_remote_dir(conn, str(remote_config), dry_run=dry_run)
        rsync_dir(
            conn,
            src_dir=local_config,
            remote_dir=str(remote_config),
            excludes=[],
            dry_run=dry_run,
        )
    else:
        log("OpenCode config not found at ~/.config/opencode; skipping config sync.")

    if local_data.exists():
        remote_data = Path(remote_home_dir) / ".local" / "share" / "opencode"
        excludes = ["auth.json"] if skip_auth else []
        ensure_remote_dir(conn, str(remote_data), dry_run=dry_run)
        rsync_dir(
            conn,
            src_dir=local_data,
            remote_dir=str(remote_data),
            excludes=excludes,
            dry_run=dry_run,
        )
    else:
        log("OpenCode data not found at ~/.local/share/opencode; skipping data sync.")


def sync_pi_state(
    conn: SSHConn,
    *,
    remote_home_dir: str,
    skip_auth: bool,
    dry_run: bool,
) -> None:
    local_paths = [
        Path.home() / ".pi",
        Path.home() / ".config" / "pi",
        Path.home() / ".local" / "share" / "pi",
    ]

    copied_any = False
    for local_path in local_paths:
        if not local_path.exists():
            continue
        rel = local_path.relative_to(Path.home())
        remote_path = Path(remote_home_dir) / rel
        excludes = ["agent/auth.json"] if skip_auth and rel == Path(".pi") else []
        ensure_remote_dir(conn, str(remote_path), dry_run=dry_run)
        rsync_dir(
            conn,
            src_dir=local_path,
            remote_dir=str(remote_path),
            excludes=excludes,
            dry_run=dry_run,
        )
        copied_any = True
    if not copied_any:
        log(
            "Pi state not found in ~/.pi, ~/.config/pi, or ~/.local/share/pi; skipping Pi sync."
        )


def sync_claude_state(
    conn: SSHConn,
    *,
    remote_home_dir: str,
    skip_auth: bool,
    dry_run: bool,
) -> None:
    local_claude = Path.home() / ".claude"
    local_claude_json = Path.home() / ".claude.json"

    if local_claude.exists():
        remote_claude = Path(remote_home_dir) / ".claude"
        excludes = [".encryption_key"] if skip_auth else []
        ensure_remote_dir(conn, str(remote_claude), dry_run=dry_run)
        rsync_dir(
            conn,
            src_dir=local_claude,
            remote_dir=str(remote_claude),
            excludes=excludes,
            dry_run=dry_run,
        )
    else:
        log(
            "Claude state not found at ~/.claude; skipping Claude state directory sync."
        )

    if skip_auth:
        return
    try_copy_file(
        conn,
        local_file=local_claude_json,
        remote_file=str(Path(remote_home_dir) / ".claude.json"),
        dry_run=dry_run,
    )


def sync_amp_state(
    conn: SSHConn,
    *,
    remote_home_dir: str,
    dry_run: bool,
) -> None:
    local_amp = Path.home() / ".amp"
    local_amp_config = Path.home() / ".config" / "amp"

    if local_amp.exists():
        remote_amp = Path(remote_home_dir) / ".amp"
        ensure_remote_dir(conn, str(remote_amp), dry_run=dry_run)
        rsync_dir(
            conn,
            src_dir=local_amp,
            remote_dir=str(remote_amp),
            excludes=[],
            dry_run=dry_run,
        )
    else:
        log("Amp state not found at ~/.amp; skipping Amp state directory sync.")

    if local_amp_config.exists():
        remote_amp_config = Path(remote_home_dir) / ".config" / "amp"
        ensure_remote_dir(conn, str(remote_amp_config), dry_run=dry_run)
        rsync_dir(
            conn,
            src_dir=local_amp_config,
            remote_dir=str(remote_amp_config),
            excludes=[],
            dry_run=dry_run,
        )
    else:
        log("Amp config not found at ~/.config/amp; skipping Amp config sync.")


def sync_cli_state(
    conn: SSHConn,
    *,
    clis: list[str],
    remote_home_dir: str,
    skip_auth: bool,
    dry_run: bool,
) -> tuple[str, ...]:
    copied: list[str] = []

    if "codex" in clis:
        sync_codex_state(
            conn,
            remote_home_dir=remote_home_dir,
            skip_auth=skip_auth,
            dry_run=dry_run,
        )
        copied.append("codex")

    if "kimi" in clis:
        sync_kimi_state(
            conn, remote_home_dir=remote_home_dir, skip_auth=skip_auth, dry_run=dry_run
        )
        copied.append("kimi")

    if "opencode" in clis:
        sync_opencode_state(
            conn, remote_home_dir=remote_home_dir, skip_auth=skip_auth, dry_run=dry_run
        )
        copied.append("opencode")

    if "pi" in clis:
        sync_pi_state(
            conn, remote_home_dir=remote_home_dir, skip_auth=skip_auth, dry_run=dry_run
        )
        copied.append("pi")

    if "claude" in clis:
        sync_claude_state(
            conn,
            remote_home_dir=remote_home_dir,
            skip_auth=skip_auth,
            dry_run=dry_run,
        )
        copied.append("claude")

    if "amp" in clis:
        sync_amp_state(conn, remote_home_dir=remote_home_dir, dry_run=dry_run)
        copied.append("amp")

    return tuple(copied)


def determine_remote_project_dir(
    *,
    cwd: Path,
    remote_home_dir: str,
    mirror_cwd: bool,
    override: str | None,
) -> str:
    if override:
        return override
    if mirror_cwd:
        return str(cwd)
    return str(Path(remote_home_dir) / cwd.name)


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
            "  uv run prime_handoff.py --kind cpu --mirror-cwd --ssh\n"
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
        "--provider",
        default=None,
        help="Optional provider filter passed to `prime availability list` (example: datacrunch, aws).",
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
            "If omitted, script uses the first numeric value from the selected availability entry."
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
        "--poll-seconds",
        type=int,
        default=5,
        help="Polling interval in seconds for pod status and SSH readiness checks.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help=(
            "Overall timeout in seconds while waiting for pod SSH details.\n"
            "Use a higher value if providers are slow to provision."
        ),
    )
    parser.add_argument(
        "--ssh-timeout-seconds",
        type=int,
        default=240,
        help=(
            "Timeout in seconds for SSH transport readiness after SSH details appear in pod status."
        ),
    )

    mirror_group = parser.add_mutually_exclusive_group()
    mirror_group.add_argument(
        "--mirror-cwd",
        dest="mirror_cwd",
        action="store_true",
        default=True,
        help=(
            "Copy project to the same absolute path as local cwd (default).\n"
            "Useful when session history is keyed by project path."
        ),
    )
    mirror_group.add_argument(
        "--no-mirror-cwd",
        dest="mirror_cwd",
        action="store_false",
        help=(
            "Copy project under remote home directory unless --remote-project-dir is provided.\n"
            "Result path becomes $HOME/<current_dir_name>."
        ),
    )
    parser.add_argument(
        "--remote-project-dir",
        default=None,
        help=(
            "Explicit remote directory for project sync.\n"
            "Overrides --mirror-cwd and --no-mirror-cwd behavior."
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
        "--project-exclude",
        action="append",
        default=[],
        help=(
            "Additional rsync exclude pattern for project sync.\n"
            "Repeat for multiple patterns. Example: --project-exclude .git --project-exclude node_modules\n"
            "The script always excludes .prime-handoff/transcripts to avoid non-native transcript artifacts."
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

    parser.add_argument(
        "--ssh",
        action="store_true",
        help=(
            "SSH into the pod at the end of the run.\nOnly valid when --node-count 1."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show actions and commands without creating pods or copying files.\n"
            "Still queries availability for realistic planning."
        ),
    )

    args = parser.parse_args(argv)
    if args.node_count < 1:
        parser.error("--node-count must be >= 1")
    if args.gpu_count < 1:
        parser.error("--gpu-count must be >= 1")
    if args.ssh and args.node_count != 1:
        parser.error("--ssh can only be used with --node-count 1")
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
        provider=args.provider,
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

    prefix = args.name_prefix or cwd.name or "handoff"
    pod_name = sanitize_pod_name(prefix, args.kind, pod_index)

    last_error: Exception | None = None
    chosen_entry: dict[str, Any] | None = None
    pod_id: str | None = None
    for entry in candidates:
        resource_id = str(entry.get("id", "")).strip()
        if not resource_id:
            continue

        disk_size = (
            args.disk_size
            if args.disk_size is not None
            else parse_first_int(entry.get("disk_gb"), 200)
        )
        vcpus = (
            args.vcpus
            if args.vcpus is not None
            else parse_first_int(entry.get("vcpus"), 8)
        )
        memory = (
            args.memory
            if args.memory is not None
            else parse_first_int(entry.get("memory_gb"), 32)
        )

        try:
            pod_id = create_pod(
                resource_id=resource_id,
                pod_name=pod_name,
                disk_size=disk_size,
                vcpus=vcpus,
                memory_gb=memory,
                image=args.image,
                team_id=args.team_id,
                dry_run=args.dry_run,
            )
            chosen_entry = entry
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue

    if not pod_id or not chosen_entry:
        raise RuntimeError(
            f"Failed to create pod from candidate list. Last error: {last_error}"
        )

    log(f"Pod created: {pod_id} (name={pod_name})")
    _, (ssh_user, ssh_host, ssh_port) = wait_for_pod_ssh(
        pod_id,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        dry_run=args.dry_run,
    )

    conn = SSHConn(user=ssh_user, host=ssh_host, port=ssh_port, key_path=key_path)
    wait_for_ssh_transport(
        conn,
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.ssh_timeout_seconds,
        dry_run=args.dry_run,
    )
    remote_home_dir = remote_home(conn, dry_run=args.dry_run)
    remote_project_dir = determine_remote_project_dir(
        cwd=cwd,
        remote_home_dir=remote_home_dir,
        mirror_cwd=args.mirror_cwd,
        override=args.remote_project_dir,
    )
    clis = parse_clis(args.clis)

    ensure_remote_tmux(conn, dry_run=args.dry_run)
    install_remote_clis(conn, clis=clis, dry_run=args.dry_run)

    if not args.skip_project:
        log(f"Syncing project {cwd} -> {remote_project_dir}")
        ensure_remote_dir(conn, remote_project_dir, dry_run=args.dry_run)
        project_excludes = merged_project_excludes(args.project_exclude)
        rsync_dir(
            conn,
            src_dir=cwd,
            remote_dir=remote_project_dir,
            excludes=project_excludes,
            dry_run=args.dry_run,
        )
    else:
        log("Skipping project sync because --skip-project is set.")

    copied_clis = sync_cli_state(
        conn,
        clis=clis,
        remote_home_dir=remote_home_dir,
        skip_auth=args.skip_auth,
        dry_run=args.dry_run,
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


def print_summary(summaries: list[PodSummary], *, dry_run: bool) -> None:
    print("\n=== Prime Handoff Complete ===")
    if dry_run:
        print("Mode: dry-run (no pods were actually created)")
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


def maybe_ssh(summary: PodSummary, *, enabled: bool, dry_run: bool) -> None:
    if not enabled:
        return
    cmd = ssh_base_args(summary.conn)
    if dry_run:
        log(f"[dry-run] {shlex.join(cmd)}")
        return
    subprocess.run(cmd)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run_cmd(["prime", "--version"])
    cwd = Path.cwd().resolve()
    key_path = resolve_ssh_key_path()
    if not args.dry_run and not key_path.exists():
        raise RuntimeError(f"Resolved SSH private key does not exist: {key_path}")

    summaries: list[PodSummary] = []
    for index in range(1, args.node_count + 1):
        log(f"Creating pod {index}/{args.node_count} ...")
        summary = create_single_pod(
            args=args, cwd=cwd, key_path=key_path, pod_index=index
        )
        summaries.append(summary)

    print_summary(summaries, dry_run=args.dry_run)
    if summaries:
        maybe_ssh(summaries[0], enabled=args.ssh, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
