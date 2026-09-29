#!/usr/bin/env python3
"""Create the Harbor project and the two robot accounts the lab uses, and store their secrets in k8s/.env.

  robot$<project>+pusher   push + pull   used by k8s/push-images.sh from the host (isolated docker config)
  robot$<project>+k3s      pull only     used by every k3s node (k8s/registries.yaml)

Robots instead of the admin user: each is scoped to one project, cannot log into the UI, and can be revoked on
its own. Harbor shows a robot secret exactly once, so this script writes it into k8s/.env (mode 600) right away;
a robot that exists but whose secret is not in .env is recreated (the old secret cannot be read back).
Admin credentials come from k8s/.env, never from the command line.
"""
from __future__ import annotations

import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

K8S = Path(__file__).resolve().parents[1]
ENV = K8S / ".env"


def load_env() -> dict[str, str]:
    out = {}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
    return out


def save_env(key: str, value: str) -> None:
    value = f"'{value}'"                        # robot names contain a literal $: quote so `. .env` does not expand it
    text = ENV.read_text()
    if re.search(rf"^{re.escape(key)}=", text, flags=re.M):
        text = re.sub(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, count=1, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV.write_text(text)
    ENV.chmod(0o600)


env = load_env()
HOST, PORT = env["HARBOR_HOST"], env.get("HARBOR_HTTPS_PORT", "8443")
PROJECT = env.get("HARBOR_PROJECT", "rdlab")
BASE = f"https://127.0.0.1:{PORT}/api/v2.0"
AUTH = "Basic " + base64.b64encode(f"admin:{env['HARBOR_ADMIN_PASSWORD']}".encode()).decode()
CTX = ssl.create_default_context(cafile=str(Path(__file__).resolve().parent / "certs" / "ca.crt"))
CTX.check_hostname = False          # we connect through 127.0.0.1; the cert also carries it as a SAN, but keep it simple


def call(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", AUTH)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            payload = r.read().decode()
            return r.status, (json.loads(payload) if payload.strip() else None)
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            pass
        return e.code, payload


status, body = call("GET", "/systeminfo")
if status != 200:
    sys.exit(f"!! cannot talk to harbor at {BASE}: {status} {body}")
print(f"==> harbor {body.get('harbor_version')} at {HOST}:{PORT}, project '{PROJECT}'")

# project ---------------------------------------------------------------------------------------------------
status, projects = call("GET", f"/projects?name={PROJECT}")
if any(p.get("name") == PROJECT for p in (projects or [])):
    print(f"    project '{PROJECT}' exists")
else:
    status, body = call("POST", "/projects", {"project_name": PROJECT, "metadata": {"public": "false"}, "storage_limit": -1})
    if status not in (200, 201):
        sys.exit(f"!! could not create project: {status} {body}")
    print(f"    project '{PROJECT}' created (private)")

# robots ----------------------------------------------------------------------------------------------------
ROBOTS = {
    "pusher": (["push", "pull"], "HARBOR_PUSH_USER", "HARBOR_PUSH_PASSWORD", "push/pull lab images from the host"),
    "k3s": (["pull"], "HARBOR_PULL_USER", "HARBOR_PULL_PASSWORD", "pull-only credential for the k3s nodes (registries.yaml)"),
}
status, robots = call("GET", "/robots?page_size=100")
existing = {r["name"]: r for r in (robots or [])}
for short, (actions, user_key, pw_key, desc) in ROBOTS.items():
    full = f"robot${PROJECT}+{short}"
    if full in existing and env.get(pw_key):
        print(f"    robot {full} exists, secret in .env")
        continue
    if full in existing:                       # secret lost: recreate (Harbor never shows it again)
        call("DELETE", f"/robots/{existing[full]['id']}")
        print(f"    robot {full} existed without a stored secret: recreated")
    status, body = call("POST", "/robots", {
        "name": short, "description": desc, "duration": -1, "level": "project", "disable": False,
        "permissions": [{"kind": "project", "namespace": PROJECT,
                         "access": [{"resource": "repository", "action": a} for a in actions]}],
    })
    if status not in (200, 201):
        sys.exit(f"!! could not create robot {full}: {status} {body}")
    save_env(user_key, body["name"])
    save_env(pw_key, body["secret"])
    print(f"    robot {body['name']} created ({'/'.join(actions)}), secret stored in k8s/.env")
