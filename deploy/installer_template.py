#!/usr/bin/env python3
"""Self-contained installer: deploys the ElevenLabs cost-poller changes to the
already-running cost-api service on this VPS. No credentials are bundled --
ELEVENLABS_AGENT_API_KEY must already exist on this server (it's the same
value 11agent_repo's own dashboard routes use) or be supplied via
--elevenlabs-api-key / a private prompt.

Server layout this script assumes (confirmed via read-only docker inspect on
2026-09-26 -- see server_update/server_run_method.md's "Cost API" section):
  - /opt/observe-deploy/docker-compose.yml + .env   (compose project dir)
  - /opt/observe                                     (build context / source tree)
  - container_name: neuroheart-cost-api, service name: cost-api

Backs up every file it overwrites and the .env before editing, rebuilds only
the cost-api image, recreates only that one container, verifies the running
service actually serves the new code, and restores everything on any failure.
Never touches chat-manager-api or postgres.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PAYLOAD = "__PAYLOAD__"  # replaced by package_installer.py with base64(json({relative_path: file_text}))

DEFAULT_DEPLOY_DIR = Path("/opt/observe-deploy")
DEFAULT_SOURCE_DIR = Path("/opt/observe")
SERVICE = "cost-api"
CONTAINER = "neuroheart-cost-api"
NEW_ENV_DEFAULTS = {
    "ELEVENLABS_AGENT_URL": "https://cakeworld.neuroheart.ai/elevenlabs-agent",
    "ELEVENLABS_POLL_INTERVAL_SECONDS": "60",
}


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=kwargs.pop("timeout", 300), **kwargs)


def load_payload() -> dict[str, str]:
    return json.loads(base64.b64decode(PAYLOAD))


def env_values(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = stripped[len("export "):] if stripped.startswith("export ") else stripped
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def update_env(original: str, updates: dict[str, str]) -> str:
    """Preserves every existing line/comment/ordering; only appends keys that
    don't already exist, or replaces the value of ones that do. Never touches
    keys not in `updates` -- same guarantee 11agent_repo's installer makes."""
    for value in updates.values():
        if "\n" in value or "\x00" in value:
            raise RuntimeError("invalid character in env value")
    lines = original.splitlines(keepends=True)
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        body = stripped[len("export "):] if stripped.startswith("export ") else stripped
        key = body.partition("=")[0].strip() if "=" in body and not stripped.startswith("#") else None
        if key and key in updates:
            prefix = "export " if stripped.startswith("export ") else ""
            out.append(f"{prefix}{key}={updates[key]}\n")
            seen.add(key)
        else:
            out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}\n")
    return "".join(out)


def backup_dir(root: Path) -> Path:
    backup = root / "elevenlabs_cost_poller_backups" / f"deploy-{time.time_ns()}"
    backup.mkdir(parents=True, mode=0o700)
    return backup


def write_files(payload: dict[str, str], source_dir: Path, backup: Path) -> list[Path]:
    written = []
    for rel_path, content in payload.items():
        target = source_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            saved = backup / "source" / rel_path
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
        target.write_text(content)
        written.append(target)
    return written


def restore_files(payload: dict[str, str], source_dir: Path, backup: Path) -> None:
    for rel_path in payload:
        target = source_dir / rel_path
        saved = backup / "source" / rel_path
        if saved.exists():
            shutil.copy2(saved, target)
        else:
            target.unlink(missing_ok=True)


def health_check(deploy_dir: Path) -> None:
    """Confirms the recreated container is actually running the new code --
    not just that the container process started. Probes the internal port
    (127.0.0.1:8005, per docker-compose.yml) rather than assuming a route."""
    for attempt in range(15):
        result = run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER])
        if result.stdout.strip() == "true":
            break
        time.sleep(2)
    else:
        raise RuntimeError("cost-api container did not report Running after rebuild")

    for attempt in range(10):
        probe = run(["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}",
                     "http://127.0.0.1:8005/internal/calls"])
        if probe.stdout.strip() in ("200", "422"):  # 422 = reachable, just missing query params
            return
        time.sleep(2)
    raise RuntimeError("cost-api did not respond on 127.0.0.1:8005 after rebuild")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy-dir", default=str(DEFAULT_DEPLOY_DIR))
    parser.add_argument("--source-dir", default=str(DEFAULT_SOURCE_DIR))
    parser.add_argument("--elevenlabs-url", default=NEW_ENV_DEFAULTS["ELEVENLABS_AGENT_URL"])
    parser.add_argument("--elevenlabs-poll-interval-seconds", default=NEW_ENV_DEFAULTS["ELEVENLABS_POLL_INTERVAL_SECONDS"])
    parser.add_argument("--elevenlabs-api-key", default="",
                         help="Same value as 11agent_repo's ELEVENLABS_AGENT_API_KEY. "
                              "If omitted and not already set in .env, you'll be prompted privately.")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("FAILED: run with sudo python3")
        return 1

    deploy_dir = Path(args.deploy_dir)
    source_dir = Path(args.source_dir)
    env_path = deploy_dir / ".env"
    compose_path = deploy_dir / "docker-compose.yml"

    if not compose_path.is_file():
        print(f"FAILED: {compose_path} not found -- wrong --deploy-dir?")
        return 1
    if not env_path.is_file():
        print(f"FAILED: {env_path} not found -- wrong --deploy-dir?")
        return 1

    existing_env = env_values(env_path.read_text())
    api_key = args.elevenlabs_api_key or existing_env.get("ELEVENLABS_AGENT_API_KEY", "")
    if not api_key:
        api_key = getpass.getpass("ELEVENLABS_AGENT_API_KEY (same value as on 11agent_repo's server): ").strip()
    if not api_key:
        print("FAILED: ELEVENLABS_AGENT_API_KEY is required")
        return 1

    payload = load_payload()
    backup = backup_dir(deploy_dir)
    print(f"Backup directory: {backup}")

    env_backup = backup / "env.original"
    shutil.copy2(env_path, env_backup)

    written = []
    env_written = False
    try:
        written = write_files(payload, source_dir, backup)
        print(f"Wrote {len(written)} file(s) under {source_dir}")

        new_env = update_env(env_path.read_text(), {
            "ELEVENLABS_AGENT_URL": args.elevenlabs_url,
            "ELEVENLABS_AGENT_API_KEY": api_key,
            "ELEVENLABS_POLL_INTERVAL_SECONDS": args.elevenlabs_poll_interval_seconds,
        })
        env_path.write_text(new_env)
        env_written = True
        print(f"Updated {env_path} (existing keys preserved)")

        print("Building cost-api image...")
        build = run(["docker", "compose", "-f", str(compose_path), "--project-directory", str(deploy_dir),
                     "build", SERVICE], timeout=600)
        if build.returncode != 0:
            print(build.stdout, build.stderr)
            raise RuntimeError("docker compose build failed")

        print("Recreating cost-api container...")
        up = run(["docker", "compose", "-f", str(compose_path), "--project-directory", str(deploy_dir),
                  "up", "-d", SERVICE], timeout=120)
        if up.returncode != 0:
            print(up.stdout, up.stderr)
            raise RuntimeError("docker compose up failed")

        health_check(deploy_dir)
    except BaseException as error:
        print(f"FAILED: {error}")
        print("Rolling back...")
        if env_written:
            shutil.copy2(env_backup, env_path)
        if written:
            restore_files(payload, source_dir, backup)
        rebuild = run(["docker", "compose", "-f", str(compose_path), "--project-directory", str(deploy_dir),
                       "build", SERVICE], timeout=600)
        run(["docker", "compose", "-f", str(compose_path), "--project-directory", str(deploy_dir),
             "up", "-d", SERVICE], timeout=120)
        if rebuild.returncode != 0:
            print("ROLLBACK BUILD ALSO FAILED -- cost-api may be down. Inspect manually:")
            print(rebuild.stdout, rebuild.stderr)
        else:
            print("Rolled back to previous source/.env and rebuilt.")
        return 1

    print("SUCCESS: cost-api rebuilt and running with the ElevenLabs cost poller.")
    print(f"Poll interval: every {args.elevenlabs_poll_interval_seconds}s against {args.elevenlabs_url}")
    print("Watch it work: docker logs -f neuroheart-cost-api")
    print("Manual one-off poll for debugging: "
          f"docker exec -it {CONTAINER} python /app/apps/cost-api/scripts/poll_elevenlabs_cost_events.py --once")
    print("Note: scripts/ is not baked into the image (see Dockerfile) -- that manual command only works "
          "if you also copy scripts/poll_elevenlabs_cost_events.py into the container or run it on the host "
          "with PYTHONPATH=app pointed at /opt/observe/apps/cost-api.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
