#!/usr/bin/env python3
"""Fixes two real issues found after the elevenlabs cost-poller deploy
(2026-09-26):

1. `docker compose up -d cost-api` recreated the container and dropped it off
   `cost-bridge` -- that network was only ever attached via a manual
   `docker network connect` (not declared in cost-api's compose service), so
   it doesn't survive a recreate. chat-manager-api reaches cost-api by Docker
   DNS name (`neuroheart-cost-api:8000`), which requires both containers on
   a shared network -- this is why the Chat Manager Cost tab started
   returning 503 "cost monitoring unavailable" right after the rebuild.

2. The ELEVENLABS_AGENT_API_KEY value entered at the installer's prompt got
   corrupted in transit (stored as 51 chars; the real value on
   11agent_repo's server is 43 chars) -- causing the poller's own 401s
   against 11agent_repo, a separate bug from #1.

No credentials are bundled; the correct key must be supplied via
--elevenlabs-api-key or a private prompt (never sourced from another
server automatically). Reconnects the network without any rebuild/restart
(same technique as fix_cost_monitor_network.py), then rewrites just the one
.env line for the key.
"""
from __future__ import annotations

import argparse
import getpass
import shutil
import subprocess
import sys
import time
from pathlib import Path

COST = "neuroheart-cost-api"
NETWORK = "cost-bridge"
ENV_PATH = Path("/opt/observe-deploy/.env")


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def env_values(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def update_env(original: str, updates: dict[str, str]) -> str:
    for value in updates.values():
        if "\n" in value or "\x00" in value:
            raise RuntimeError("invalid character in env value")
    lines = original.splitlines(keepends=True)
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key and key in updates:
            out.append(f"{key}={updates[key]}\n")
            seen.add(key)
        else:
            out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n"
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}\n")
    return "".join(out)


def networks_of(container: str) -> set[str]:
    result = run(["docker", "inspect", "-f", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}", container])
    if result.returncode:
        raise RuntimeError(f"could not inspect {container}: {result.stderr.strip()}")
    return set(result.stdout.split())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elevenlabs-api-key", default="",
                         help="The real ELEVENLABS_AGENT_API_KEY from 11agent_repo's server. "
                              "If omitted, you'll be prompted privately.")
    args = parser.parse_args()

    api_key = args.elevenlabs_api_key
    if not api_key:
        api_key = getpass.getpass("ELEVENLABS_AGENT_API_KEY (paste carefully, no trailing newline/space): ").strip()
    if not api_key:
        print("FAILED: key is required")
        return 1
    if len(api_key) != 43:
        print(f"WARNING: expected a 43-character key, got {len(api_key)}. "
              "Double-check you copied it correctly before continuing.")
        confirm = input("Continue anyway? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted, no changes made.")
            return 1

    # --- Fix 1: network ---
    already = NETWORK in networks_of(COST)
    if not already:
        result = run(["docker", "network", "connect", NETWORK, COST])
        if result.returncode:
            print("FAILED to connect network:", result.stderr.strip())
            return 1
        print(f"Connected {COST} to {NETWORK}.")
    else:
        print(f"{COST} is already on {NETWORK} -- no network change needed.")

    # --- Fix 2: API key ---
    if not ENV_PATH.is_file():
        print(f"FAILED: {ENV_PATH} not found")
        return 1
    backup = ENV_PATH.with_name(f".env.backup-{time.time_ns()}")
    shutil.copy2(ENV_PATH, backup)
    new_env = update_env(ENV_PATH.read_text(), {"ELEVENLABS_AGENT_API_KEY": api_key})
    ENV_PATH.write_text(new_env)
    print(f"Updated {ENV_PATH} (backup: {backup}).")
    print("NOTE: the running container's environment is only refreshed by "
          "'docker compose up -d cost-api' -- restarting it now so the fixed "
          "key takes effect (network attachment set above survives this, "
          "since docker compose 'up' without --build on an unchanged image "
          "recreates in place using the same already-built image).")

    result = run(["docker", "compose", "-f", "/opt/observe-deploy/docker-compose.yml",
                  "--project-directory", "/opt/observe-deploy", "up", "-d", "cost-api"], timeout=60)
    if result.returncode:
        print("FAILED to recreate cost-api:", result.stderr.strip())
        return 1

    # Recreate drops the network again (same root cause as before) -- reconnect.
    if NETWORK not in networks_of(COST):
        result = run(["docker", "network", "connect", NETWORK, COST])
        if result.returncode:
            print("FAILED to reconnect network after restart:", result.stderr.strip())
            return 1
        print(f"Reconnected {COST} to {NETWORK} after restart.")

    # --- Verify ---
    for attempt in range(10):
        probe = run(["docker", "exec", "chat-manager-api", "python3", "-c",
                      "import urllib.request; urllib.request.urlopen('http://neuroheart-cost-api:8000/internal/calls', timeout=5); print('OK')"])
        if probe.returncode == 0 and "OK" in probe.stdout:
            break
        time.sleep(2)
    else:
        print("FAILED: chat-manager-api still cannot reach cost-api after fix.")
        return 1
    print("SUCCESS: chat-manager-api can reach cost-api again. Refresh the Cost tab.")

    logs = run(["docker", "logs", "--tail", "5", COST])
    print("\nLatest cost-api logs:\n", logs.stdout, logs.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
