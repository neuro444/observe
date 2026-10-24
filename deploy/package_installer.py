#!/usr/bin/env python3
"""Regenerate deploy/dist/setup_elevenlabs_cost_poller.py by embedding the
ElevenLabs cost-ingestion source files into installer_template.py.

Run this after any source change, then scp the output to the server:

    python3 deploy/package_installer.py
    scp deploy/dist/setup_elevenlabs_cost_poller.py root@159.198.44.98:/tmp/
    ssh root@159.198.44.98 'sudo python3 /tmp/setup_elevenlabs_cost_poller.py'
"""
import base64
import json
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent
TEMPLATE = REPO_ROOT / "deploy" / "installer_template.py"
DIST_DIR = REPO_ROOT / "deploy" / "dist"
OUTPUT = DIST_DIR / "setup_elevenlabs_cost_poller.py"

# Only the files this deploy actually needs to add/change on the server.
# Paths are relative to /opt/observe (the build context), matching this
# repo's own layout exactly.
FILES = [
    "apps/cost-api/app/main.py",
    "apps/cost-api/app/elevenlabs_poller.py",
    "apps/cost-api/scripts/poll_elevenlabs_cost_events.py",
    "packages/cost-engine/cost_engine/calculate.py",
]


def build() -> pathlib.Path:
    payload = {name: (REPO_ROOT / name).read_text() for name in FILES}
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()

    template_text = TEMPLATE.read_text()
    marker = 'PAYLOAD = "__PAYLOAD__"'
    if marker not in template_text:
        raise RuntimeError("installer_template.py is missing the expected PAYLOAD marker")
    output_text = template_text.replace(marker, f'PAYLOAD = "{encoded}"')

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(output_text)
    OUTPUT.chmod(0o700)
    return OUTPUT


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path} ({path.stat().st_size} bytes)")
    print("Next:")
    print(f"  scp {path} root@159.198.44.98:/tmp/")
    print(f"  ssh root@159.198.44.98 'sudo python3 /tmp/{path.name}'")
