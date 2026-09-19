import base64
import hmac
import html
import json
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
TARGETS = Path(os.environ.get("PROM_TARGETS_DIR", "/targets"))
TARGETS.mkdir(parents=True, exist_ok=True)

pool = ConnectionPool(
    os.environ["CONTROL_DATABASE_URL"],
    min_size=1,
    max_size=5,
    open=False,
    kwargs={"row_factory": dict_row},
)
AUTH_MODE = os.environ.get("CONTROL_AUTH_MODE", "basic").strip().lower()  # basic | authentik
AUTHENTIK_JWT_SECRET = os.environ.get("CONTROL_AUTHENTIK_JWT_SECRET", "")
if AUTH_MODE == "authentik" and not AUTHENTIK_JWT_SECRET:
    raise SystemExit("CONTROL_AUTH_MODE=authentik exige CONTROL_AUTHENTIK_JWT_SECRET (client_secret do provider proxy).")
security = HTTPBasic(auto_error=AUTH_MODE != "authentik")
fernet = Fernet(os.environ["CONTROL_MASTER_KEY"].encode())
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def now():
    return datetime.now(timezone.utc)


def db(query, args=(), one=False):
    with pool.connection() as conn:
        cur = conn.execute(query, args)
        if cur.description:
            return cur.fetchone() if one else cur.fetchall()


def require_admin(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    if AUTH_MODE == "authentik":
        # Atras do forward auth do Traefik: o outpost so deixa passar quem esta no grupo da application e
        # injeta X-authentik-jwt, assinado em HS256 com o client_secret do provider (Authentik 2026.8).
        # Validar o JWT garante que a requisicao veio pelo outpost, e nao direto na porta do container.
        token = request.headers.get("x-authentik-jwt", "")
        try:
            claims = jwt.decode(token, AUTHENTIK_JWT_SECRET, algorithms=["HS256"], options={"verify_aud": False})
        except jwt.PyJWTError:
            raise HTTPException(401, "Sessão do SSO ausente ou inválida.")
        return claims.get("preferred_username") or claims.get("email") or claims.get("sub") or "sso"
    expected_user = os.environ["CONTROL_ADMIN_EMAIL"]
    expected_pass = os.environ["CONTROL_ADMIN_PASSWORD"]
    if credentials is None or not (
        hmac.compare_digest(credentials.username.encode(), expected_user.encode())
        and hmac.compare_digest(credentials.password.encode(), expected_pass.encode())
    ):
        raise HTTPException(401, "Credenciais inválidas.", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@app.on_event("startup")
def startup():
    pool.open()
    pool.wait()
    db((APP_DIR / "schema.sql").read_text())
    sync_targets()


@app.on_event("shutdown")
def shutdown():
    pool.close()


class CustomerIn(BaseModel):
    company_name: str = Field(min_length=2, max_length=120)
    slug: str = Field(default="", max_length=50)
    domain: str = Field(default="", max_length=253)
    admin_email: str = Field(min_length=5, max_length=254)
    endpoint_id: int | None = None


def slugify(value: str):
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if not value or len(value) > 50:
        raise HTTPException(422, "Slug inválido.")
    return value


def allocate_subnet():
    prefix = os.environ.get("TENANT_SUBNET_PREFIX", "10.88")
    used = {str(r["subnet"]) for r in db("SELECT subnet FROM customers")}
    for slot in range(1, 251):
        subnet = f"{prefix}.{slot}.0/24"
        if subnet not in used:
            return subnet
    raise HTTPException(507, "Não há sub-redes disponíveis no pool configurado.")


def encrypt_secrets(data: dict):
    return fernet.encrypt(json.dumps(data, separators=(",", ":")).encode())


def decrypt_secrets(value):
    return json.loads(fernet.decrypt(bytes(value)).decode())


def new_secret(n=32):
    return secrets.token_urlsafe(n)


def customer_public(row):
    return {
        "id": str(row["id"]),
        "company_name": row["company_name"],
        "slug": row["slug"],
        "domain": row["domain"],
        "stack_name": row["stack_name"],
        "stack_id": row["stack_id"],
        "endpoint_id": row["endpoint_id"],
        "subnet": row["subnet"],
        "admin_email": row["admin_email"],
        "status": row["status"],
        "last_error": row["last_error"],
        "created_at": row["created_at"].isoformat(),
        "deployed_at": row["deployed_at"].isoformat() if row["deployed_at"] else None,
    }


def sync_targets():
    rows = db("SELECT company_name,slug,domain FROM customers WHERE status='active' ORDER BY slug")
    payload = [{
        "targets": [r["domain"] + ":443"],
        "labels": {"tenant": r["slug"], "company": r["company_name"]},
    } for r in rows]
    tmp = TARGETS / "tenants.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(TARGETS / "tenants.json")


def portainer_headers():
    return {"X-API-Key": os.environ["PORTAINER_API_KEY"], "Content-Type": "application/json"}


def tenant_env(row, secrets_data):
    slug = row["slug"]
    stack = row["stack_name"]
    return {
        "STACK_NAME": stack,
        "TENANT_SLUG": slug,
        "APP_HOST": row["domain"],
        "APP_ORIGIN": "https://" + row["domain"],
        "ROUTER_NAME": stack,
        "ADMIN_EMAIL": row["admin_email"],
        "ADMIN_PASSWORD": secrets_data["admin_password"],
        "JWT_SECRET": secrets_data["jwt_secret"],
        "POSTGRES_PASSWORD": secrets_data["postgres_password"],
        "REDIS_PASSWORD": secrets_data["redis_password"],
        "TURN_HOST": os.environ["SHARED_TURN_HOST"],
        "TURN_SECRET": os.environ["SHARED_TURN_SECRET"],
        "METRICS_TOKEN": os.environ["METRICS_TOKEN"],
        "DATABASE_VOLUME": f"{stack}_database",
        "PHOTOS_VOLUME": f"{stack}_photos",
        "REDIS_VOLUME": f"{stack}_redis",
        "INTERNAL_NETWORK": f"{stack}_internal",
        "INTERNAL_SUBNET": row["subnet"],
        "PROXY_NETWORK": os.environ.get("PROXY_NETWORK", "traefik"),
        "TRAEFIK_ENTRYPOINT": os.environ.get("TRAEFIK_ENTRYPOINT", "websecure"),
        "TRAEFIK_CERTRESOLVER": os.environ.get("TRAEFIK_CERTRESOLVER", "letsencrypt"),
    }


def git_payload(row, env):
    body = {
        "Name": row["stack_name"],
        "RepositoryURL": os.environ["SUPPORT_REPO_URL"],
        "RepositoryReferenceName": os.environ.get("SUPPORT_REPO_REF", "refs/heads/main"),
        "ComposeFile": os.environ.get("SUPPORT_COMPOSE_FILE", "compose.tenant.yaml"),
        "Env": [{"name": k, "value": str(v)} for k, v in env.items()],
    }
    source_id = int(os.environ.get("PORTAINER_SOURCE_ID", "0") or 0)
    credential_id = int(os.environ.get("PORTAINER_GIT_CREDENTIAL_ID", "0") or 0)
    if source_id:
        # Portainer 2.43+ replaced shared Git credentials with Sources.
        body["SourceID"] = source_id
        body.pop("RepositoryURL", None)
    elif credential_id:
        # Compatibility with older Portainer installations.
        body["RepositoryAuthentication"] = True
        body["RepositoryGitCredentialID"] = credential_id
    elif os.environ.get("SUPPORT_GIT_TOKEN"):
        body.update({
            "RepositoryAuthentication": True,
            "RepositoryUsername": os.environ.get("SUPPORT_GIT_USERNAME", ""),
            "RepositoryPassword": os.environ["SUPPORT_GIT_TOKEN"],
        })
    return body


PORTAINER_DEPLOY_TIMEOUT = float(os.environ.get("PORTAINER_DEPLOY_TIMEOUT", "900"))


async def find_stack_by_name(name):
    """A stack pode existir mesmo quando a resposta do create se perdeu (timeout): recupera pelo nome."""
    url = os.environ["PORTAINER_URL"].rstrip("/") + "/api/stacks"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers=portainer_headers())
    if response.status_code != 200:
        return None
    return next((s for s in response.json() if s.get("Name") == name), None)


async def deploy_row(row):
    secrets_data = decrypt_secrets(row["secrets_encrypted"])
    endpoint_id = int(row["endpoint_id"])
    url = os.environ["PORTAINER_URL"].rstrip("/") + f"/api/stacks/create/standalone/repository?endpointId={endpoint_id}"
    try:
        # O Portainer so responde depois do `compose up --build`; o primeiro build da imagem leva minutos.
        async with httpx.AsyncClient(timeout=PORTAINER_DEPLOY_TIMEOUT) as client:
            response = await client.post(url, headers=portainer_headers(), json=git_payload(row, tenant_env(row, secrets_data)))
    except httpx.HTTPError as exc:
        existing = await find_stack_by_name(row["stack_name"])
        if existing:
            result = existing
        else:
            detail = f"Sem resposta do Portainer: {exc.__class__.__name__}"
            db("UPDATE customers SET status='error',last_error=%s,updated_at=now() WHERE id=%s", (detail, row["id"]))
            raise HTTPException(502, detail)
    else:
        if response.status_code >= 300:
            detail = response.text[:1000]
            db("UPDATE customers SET status='error',last_error=%s,updated_at=now() WHERE id=%s", (detail, row["id"]))
            raise HTTPException(502, "Portainer recusou o deploy: " + detail)
        result = response.json()
    stack_id = result.get("Id")
    db("UPDATE customers SET stack_id=%s,status='active',last_error='',deployed_at=now(),updated_at=now() WHERE id=%s",
       (stack_id, row["id"]))
    sync_targets()
    return stack_id


@app.get("/api/health")
def health():
    db("SELECT 1")
    return {"status": "ok"}


@app.get("/api/customers")
def customers(_: str = Depends(require_admin)):
    return [customer_public(r) for r in db("SELECT * FROM customers ORDER BY company_name")]


@app.post("/api/customers")
async def create_customer(data: CustomerIn, _: str = Depends(require_admin)):
    slug = slugify(data.slug or data.company_name)
    base_domain = os.environ.get("BASE_DOMAIN", "suporte.krato.ai").strip(".")
    domain = (data.domain or f"{slug}.{base_domain}").lower().strip()
    if not re.fullmatch(r"[a-z0-9.-]+", domain) or "." not in domain:
        raise HTTPException(422, "Domínio inválido.")
    if db("SELECT id FROM customers WHERE slug=%s OR domain=%s", (slug, domain), one=True):
        raise HTTPException(409, "Já existe cliente com esse slug ou domínio.")

    cid = str(uuid.uuid4())
    stack = "sr-" + slug
    endpoint_id = data.endpoint_id or int(os.environ["PORTAINER_ENDPOINT_ID"])
    secret_data = {
        "admin_password": new_secret(18),
        "jwt_secret": new_secret(48),
        "postgres_password": new_secret(32),
        "redis_password": new_secret(32),
    }
    subnet = allocate_subnet()
    db("""INSERT INTO customers(id,company_name,slug,domain,stack_name,endpoint_id,subnet,admin_email,secrets_encrypted)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
       (cid, data.company_name.strip(), slug, domain, stack, endpoint_id, subnet, data.admin_email.lower().strip(), encrypt_secrets(secret_data)))
    row = db("SELECT * FROM customers WHERE id=%s", (cid,), one=True)
    try:
        stack_id = await deploy_row(row)
    except HTTPException:
        raise
    return {
        **customer_public(db("SELECT * FROM customers WHERE id=%s", (cid,), one=True)),
        "stack_id": stack_id,
        "initial_admin_password": secret_data["admin_password"],
        "password_notice": "Esta senha é exibida somente nesta resposta. Guarde-a em local seguro.",
        "dns_notice": f"Se usar o domínio padrão, configure wildcard *.{base_domain} para a VPS. Em domínio próprio, aponte DNS para a VPS antes do deploy.",
    }


@app.post("/api/customers/{customer_id}/redeploy")
async def redeploy(customer_id: str, _: str = Depends(require_admin)):
    row = db("SELECT * FROM customers WHERE id=%s", (customer_id,), one=True)
    if not row or not row["stack_id"]:
        raise HTTPException(404, "Cliente/stack não encontrado.")
    endpoint_id = int(row["endpoint_id"])
    url = os.environ["PORTAINER_URL"].rstrip("/") + f"/api/stacks/{row['stack_id']}/git/redeploy?endpointId={endpoint_id}"
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.put(url, headers=portainer_headers(), json={"PullImage": True, "Prune": False})
    if response.status_code >= 300:
        raise HTTPException(502, response.text[:1000])
    db("UPDATE customers SET status='active',last_error='',updated_at=now() WHERE id=%s", (customer_id,))
    return {"ok": True}


async def prom_query(query: str):
    url = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/") + "/api/v1/query"
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(url, params={"query": query})
    if response.status_code != 200:
        return []
    return response.json().get("data", {}).get("result", [])


def metric_value(result, default=0.0):
    try:
        return float(result[0]["value"][1]) if result else default
    except (KeyError, IndexError, TypeError, ValueError):
        return default


@app.get("/api/customers/{customer_id}/monitor")
async def monitor(customer_id: str, _: str = Depends(require_admin)):
    row = db("SELECT * FROM customers WHERE id=%s", (customer_id,), one=True)
    if not row:
        raise HTTPException(404, "Cliente não encontrado.")
    tenant = row["slug"]
    stack = row["stack_name"]
    active = await prom_query(f'support_active_sessions{{tenant="{tenant}"}}')
    ws = await prom_query(f'support_websocket_peers{{tenant="{tenant}"}}')
    turn = await prom_query(f'support_webrtc_turn_sessions{{tenant="{tenant}"}}')
    direct = await prom_query(f'support_webrtc_direct_sessions{{tenant="{tenant}"}}')
    quality = {q: metric_value(await prom_query(f'support_quality_sessions{{tenant="{tenant}",quality="{q}"}}'))
               for q in ("good", "unstable", "bad")}
    online = await prom_query(f'up{{job="tenant-app",tenant="{tenant}"}}')
    cpu = await prom_query(f'sum(rate(container_cpu_usage_seconds_total{{container_label_com_docker_compose_project="{stack}"}}[2m])) * 100')
    memory = await prom_query(f'sum(container_memory_working_set_bytes{{container_label_com_docker_compose_project="{stack}"}})')
    rx = await prom_query(f'sum(rate(container_network_receive_bytes_total{{container_label_com_docker_compose_project="{stack}"}}[2m]))')
    tx = await prom_query(f'sum(rate(container_network_transmit_bytes_total{{container_label_com_docker_compose_project="{stack}"}}[2m]))')
    return {
        "active_sessions": int(metric_value(active)),
        "websocket_peers": int(metric_value(ws)),
        "turn_sessions": int(metric_value(turn)),
        "direct_sessions": int(metric_value(direct)),
        "quality": {k: int(v) for k, v in quality.items()},
        "online": bool(online) and metric_value(online) == 1.0,
        "cpu_percent": round(metric_value(cpu), 2),
        "memory_mb": round(metric_value(memory) / 1024 / 1024, 1),
        "network_rx_mbps": round(metric_value(rx) * 8 / 1_000_000, 2),
        "network_tx_mbps": round(metric_value(tx) * 8 / 1_000_000, 2),
    }


@app.get("/api/me")
def me(user: str = Depends(require_admin)):
    return {"user": user, "auth_mode": AUTH_MODE}


@app.get("/logout")
def logout():
    if AUTH_MODE == "authentik":
        return RedirectResponse("/outpost.goauthentik.io/sign_out", status_code=302)
    return JSONResponse({"detail": "Feche o navegador para encerrar a sessão (basic auth)."}, status_code=200)


@app.get("/", response_class=HTMLResponse)
def dashboard(_: str = Depends(require_admin)):
    return (APP_DIR / "static" / "index.html").read_text()
