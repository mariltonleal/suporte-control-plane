# Suporte Remoto — Control Plane (painel central)

Painel central que **cadastra, provisiona e monitora** instalações single-tenant do Suporte Remoto
(atendimento técnico remoto por vídeo). Cada empresa recebe uma stack própria (app + PostgreSQL + Redis +
volumes + rede isolada + domínio + segredos), criada pela API do Portainer; o monitoramento é
Prometheus + Grafana + docker-exporter + node-exporter.

Este repositório é um **recorte publicado** da pasta do control plane do produto (o produto em si é privado).
Serve de referência para quem constrói um front-end novo para o painel.

## Mapa

| Caminho | O que é |
|---|---|
| `control-plane/app.py` | API FastAPI do painel (cadastro de empresas, provisionamento via Portainer, resumo de métricas via Prometheus) |
| `control-plane/static/index.html` | Front atual (HTML/JS puro, uma página) — é o que um front novo substitui |
| `control-plane/schema.sql` | Tabela `customers` |
| `compose.control-plane.yaml` | Stack do painel + monitoramento |
| `compose.tenant.yaml` | Template da stack de CADA empresa (o painel preenche as variáveis) |
| `monitoring/` | Prometheus (scrape config), Grafana (datasource + dashboard), docker-exporter |
| `tools/loadtest/` | Gerador de carga sintético e amostrador de recursos |
| `ops/control-plane-stack.py` | Cria/redeploya a stack do painel pela API do Portainer |
| `docs/control-plane.md` | Arquitetura, decisões e gotchas de deploy |
| `.env.control-plane.example` | Variáveis da stack |

## API do painel (o contrato para o front)

Autenticação: `CONTROL_AUTH_MODE=basic` (HTTP Basic) **ou** `CONTROL_AUTH_MODE=authentik` (o painel fica atrás
do forward auth do Authentik via Traefik e valida o header `X-authentik-jwt`, HS256 com o client_secret do
provider). No modo authentik o front não faz login: a sessão vem do proxy; "sair" = `GET /logout`.

| Método e rota | Resposta |
|---|---|
| `GET /api/health` | `{"status":"ok"}` (sem auth) |
| `GET /api/me` | `{"user": "<usuário>", "auth_mode": "basic|authentik"}` |
| `GET /api/customers` | lista de empresas (ver objeto abaixo) |
| `POST /api/customers` | body `{"company_name","slug"?,"domain"?,"admin_email","endpoint_id"?}` → empresa + `initial_admin_password` (aparece **só** nesta resposta), `password_notice`, `dns_notice`. Cria a stack no Portainer (assíncrono: `status` fica `active` antes de a stack terminar; use `monitor.online` para saber se respondeu) |
| `POST /api/customers/{id}/redeploy` | `{"ok": true}` — git pull + rebuild da stack da empresa |
| `GET /api/customers/{id}/monitor` | `{"active_sessions","websocket_peers","turn_sessions","direct_sessions","quality":{"good","unstable","bad"},"online","cpu_percent","memory_mb","network_rx_mbps","network_tx_mbps"}` |
| `GET /logout` | modo authentik: 302 para `/outpost.goauthentik.io/sign_out` |
| `GET /` | o front atual |

Objeto empresa:

```json
{"id":"uuid","company_name":"ACME","slug":"acme","domain":"acme.suporte.exemplo.com","stack_name":"sr-acme",
 "stack_id":3,"endpoint_id":1,"subnet":"10.88.1.0/24","admin_email":"admin@acme.com","status":"active|error|pending",
 "last_error":"","created_at":"ISO-8601","deployed_at":"ISO-8601|null"}
```

Erros vêm como `{"detail": "mensagem"}` com 401/404/409/422/502/507. Não há paginação (lista pequena).

## Métricas por empresa (o que a aplicação do cliente expõe em `/api/ops/metrics`, Bearer token)

`support_active_sessions`, `support_waiting_sessions`, `support_invited_sessions`, `support_rooms_loaded`,
`support_websocket_peers`, `support_panel_websockets`, `support_webrtc_direct_sessions`,
`support_webrtc_turn_sessions`, `support_quality_sessions{quality="good|unstable|bad"}`,
`support_http_5xx_total`, `support_ws_rejected_total`, `support_process_start_time_seconds`.
No Prometheus cada série ganha os labels `tenant` (slug) e `company`. Recursos por container vêm do
docker-exporter com nomes compatíveis com o cAdvisor (`container_cpu_usage_seconds_total`,
`container_memory_working_set_bytes`, `container_network_*`, labels `container_label_krato_tenant` etc.).

## Como rodar localmente (só o painel)

```bash
docker network create traefik   # o compose espera a rede do proxy
cp .env.control-plane.example .env
# preencher CONTROL_MASTER_KEY (Fernet), CONTROL_DB_PASSWORD, PORTAINER_* etc.
docker compose --env-file .env -f compose.control-plane.yaml up -d --build
```

Sem Portainer/Prometheus de verdade o painel sobe, lista e cadastra (o deploy falha com 502 e a empresa fica
`error`), o que basta para desenvolver o front.

Licença: todos os direitos reservados (código publicado apenas para leitura/referência).
