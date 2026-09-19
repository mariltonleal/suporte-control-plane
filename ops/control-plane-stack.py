#!/usr/bin/env python3
"""Cria ou atualiza a stack do Control Plane no Portainer a partir de um arquivo de variaveis.

Uso (na VPS, como root):
  ops/control-plane-stack.py status   [--env-file /root/.secrets/suporte-control-plane.env]
  ops/control-plane-stack.py create   [--ref refs/heads/main] [--stack suporte-control-plane]
  ops/control-plane-stack.py redeploy           # git pull + rebuild (recria os containers)
  ops/control-plane-stack.py update-env         # regrava as variaveis da stack (sem redeploy)

- Autentica com o token de API do arquivo (PORTAINER_API_KEY); nunca imprime segredos.
- Usa a Source Git do Portainer (PORTAINER_SOURCE_ID) — o PAT do GitHub fica so no Portainer.
- Fala com o Portainer pela URL do arquivo (PORTAINER_URL); dentro da rede `traefik` e http://portainer:9000.
"""
import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request

COMPOSE = "compose.control-plane.yaml"
# Variaveis que a stack precisa e que nao sao segredos de runtime do container (Compose le no `config`).
STACK_KEYS = [
    "CONTROL_HOST", "GRAFANA_HOST", "CONTROL_ADMIN_EMAIL", "CONTROL_ADMIN_PASSWORD", "CONTROL_MASTER_KEY",
    "CONTROL_DB_PASSWORD", "PORTAINER_URL", "PORTAINER_API_KEY", "PORTAINER_ENDPOINT_ID", "PORTAINER_SOURCE_ID",
    "PORTAINER_GIT_CREDENTIAL_ID", "SUPPORT_REPO_URL", "SUPPORT_REPO_REF", "SUPPORT_COMPOSE_FILE",
    "SUPPORT_GIT_USERNAME", "SUPPORT_GIT_TOKEN", "BASE_DOMAIN", "PROXY_NETWORK", "TRAEFIK_ENTRYPOINT",
    "TRAEFIK_CERTRESOLVER", "SHARED_TURN_HOST", "SHARED_TURN_SECRET", "METRICS_TOKEN", "CONTROL_SUBNET",
    "TENANT_SUBNET_PREFIX", "PROMETHEUS_RETENTION", "GRAFANA_ADMIN_USER", "GRAFANA_ADMIN_PASSWORD",
    "PORTAINER_DEPLOY_TIMEOUT", "NODE_EXPORTER_PORT", "NODE_EXPORTER_PASSWORD", "NODE_EXPORTER_PASSWORD_HASH_B64",
    "CONTROL_AUTH_MODE", "CONTROL_AUTHENTIK_JWT_SECRET", "CONTROL_AUTH_MIDDLEWARE", "CONTROL_AUTH_OUTPOST_SERVICE",
    "AUTHENTIK_URL", "GRAFANA_OAUTH_ENABLED", "GRAFANA_OAUTH_AUTO_LOGIN", "GRAFANA_OAUTH_NAME", "GRAFANA_OAUTH_CLIENT_ID",
    "GRAFANA_OAUTH_CLIENT_SECRET", "GRAFANA_OAUTH_APP_SLUG", "GRAFANA_OAUTH_ADMIN_GROUP", "GRAFANA_OAUTH_VIEWER_GROUP",
]


def read_env(path):
    env = {}
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def portainer_base(env):
    url = env["PORTAINER_URL"].rstrip("/")
    if url.startswith("http://portainer:"):
        # Fora da rede docker o nome `portainer` nao resolve: usa o IP do container na rede traefik.
        ip = subprocess.check_output(["docker", "inspect", "portainer", "--format",
                                      '{{(index .NetworkSettings.Networks "traefik").IPAddress}}'], text=True).strip()
        url = "http://" + ip + ":9000"
    return url + "/api"


def call(env, path, data=None, method=None, timeout=1200):
    headers = {"X-API-Key": env["PORTAINER_API_KEY"]}
    body = None
    if data is not None:
        body, headers["Content-Type"] = json.dumps(data).encode(), "application/json"
    req = urllib.request.Request(portainer_base(env) + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise SystemExit("ERRO API %s %s: %s" % (e.code, path, e.read().decode(errors="replace")[:1500]))


def stack_env(env):
    return [{"name": k, "value": env[k]} for k in STACK_KEYS if k in env]


def find_stack(env, name):
    return next((s for s in call(env, "/stacks") if s["Name"] == name), None)


def show(s):
    g = s.get("GitConfig") or {}
    print("stack", s["Id"], s["Name"], "status", s["Status"], "file", s.get("EntryPoint"),
          "ref", g.get("ReferenceName"), "source", g.get("SourceID"), "vars", len(s.get("Env") or []))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["status", "create", "redeploy", "update-env"])
    p.add_argument("--env-file", default="/root/.secrets/suporte-control-plane.env")
    p.add_argument("--stack", default="suporte-control-plane")
    p.add_argument("--ref", default=None, help="refs/heads/<branch>; padrao = SUPPORT_REPO_REF do arquivo")
    a = p.parse_args()
    env = read_env(a.env_file)
    ref = a.ref or env.get("SUPPORT_REPO_REF", "refs/heads/main")
    endpoint = int(env.get("PORTAINER_ENDPOINT_ID", "1"))
    current = find_stack(env, a.stack)

    if a.action == "status":
        print("portainer", call(env, "/status")["Version"])
        show(current) if current else print("stack", a.stack, "nao existe")
        return
    if a.action == "create":
        if current:
            raise SystemExit("ja existe; use redeploy/update-env")
        payload = {"Name": a.stack, "SourceID": int(env["PORTAINER_SOURCE_ID"]), "RepositoryReferenceName": ref,
                   "ComposeFile": COMPOSE, "Env": stack_env(env)}
        print("criando stack (o Portainer so responde depois do build; pode levar minutos)...")
        show(call(env, f"/stacks/create/standalone/repository?endpointId={endpoint}", payload, method="POST"))
        return
    if not current:
        raise SystemExit("stack nao existe; use create")
    if a.action == "update-env":
        show(call(env, f"/stacks/{current['Id']}/git?endpointId={endpoint}",
                  {"Env": stack_env(env), "RepositoryReferenceName": ref}, method="PUT"))
        return
    show(call(env, f"/stacks/{current['Id']}/git/redeploy?endpointId={endpoint}",
              {"RepositoryReferenceName": ref, "Env": stack_env(env), "PullImage": True, "Prune": False}, method="PUT"))


if __name__ == "__main__":
    main()
