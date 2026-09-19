# Control Plane e monitoramento

Este conjunto adiciona uma camada central para administrar várias instalações **single-tenant**
do Suporte Remoto. Cada empresa continua isolada: aplicação, PostgreSQL, Redis, rede interna e
volumes próprios. O painel central apenas provisiona e observa as instalações.

## Arquitetura

```text
                    controle.suporte.krato.ai
                              |
                      Control Plane
                    /        |        \
              Portainer   Prometheus   Grafana
                  |            |
        +---------+-----+      +------------------+
        |               |                         |
     Cliente A        Cliente B                 VPS/containers
   app/db/redis      app/db/redis            node-exporter/cAdvisor
        |               |
        +------- TURN compartilhado -------+
```

O TURN é compartilhado por VPS/região. Não suba um Coturn em cada stack na mesma VPS usando
3478/5349, pois as portas entram em conflito. Dados de aplicação não são compartilhados entre
clientes; o TURN apenas retransmite mídia quando WebRTC direto não é possível.

## O que o painel faz

- cadastra empresa, slug, domínio e e-mail do administrador;
- gera senhas/segredos fortes por empresa;
- reserva uma sub-rede /24 exclusiva;
- cria a stack pelo Portainer usando o repositório Git;
- cria volumes exclusivos de banco, fotos e Redis;
- registra o domínio no Prometheus automaticamente;
- exibe atendimentos ativos, WebSockets, chamadas via TURN, CPU, RAM e rede;
- permite solicitar redeploy Git da stack.

A senha inicial do administrador é devolvida somente na criação. Os demais segredos ficam
criptografados no banco do control plane com `CONTROL_MASTER_KEY`.

## Domínios

Para clientes usando o domínio padrão, a opção mais simples é criar um DNS wildcard:

```
*.suporte.krato.ai -> IP da VPS
```

Assim, ao cadastrar `ACME`, o painel pode criar automaticamente:

```
acme.suporte.krato.ai
```

O Traefik continua emitindo o certificado TLS para cada host.

Para domínio próprio, por exemplo `suporte.acme.com.br`, o cliente precisa apontar o DNS para
a VPS antes da emissão do certificado.

## Portainer

Crie um usuário/token de API com permissão somente sobre o environment onde as stacks de
clientes serão provisionadas. Configure `PORTAINER_API_KEY` e `PORTAINER_ENDPOINT_ID`.

Para Portainer 2.43 ou superior, prefira cadastrar o repositório/credencial como **Source** no
Portainer e informar `PORTAINER_SOURCE_ID`. O painel mantém compatibilidade com
`PORTAINER_GIT_CREDENTIAL_ID` em versões antigas e, como último recurso, usuário/PAT inline.
Isso evita guardar um PAT do GitHub no control plane.

Conferido no Portainer CE 2.45.1: a API de Sources fica em `/api/gitops/sources`
(`GET` lista; `POST /api/gitops/sources/git` cria com `{"name","url","authentication":{"username","password"}}`).
O create de stack por repositório aceita `SourceID` e, quando ele é informado, `RepositoryURL` e as
credenciais inline são ignorados (marcados como *deprecated* no código do Portainer). O token de API do
control plane é criado em `POST /api/users/<id>/tokens` (`{"description","password"}`) e a chave crua só
aparece nessa resposta.

No Portainer 2.45.1 o create de stack por repositório responde em ~1 s com `Status: 3` (implantação em
andamento) e o clone/build/`up` seguem em segundo plano — o cliente aparece como `active` no painel antes
de a stack terminar de subir; o indicador "Coleta online" (scrape do Prometheus) mostra quando ela
realmente respondeu. Em versões que respondem só ao final, `PORTAINER_DEPLOY_TIMEOUT` (padrão 900 s)
cobre o build da imagem; se a resposta se perder, o painel procura a stack pelo nome antes de marcar erro.

O provisionamento usa:

```
POST /api/stacks/create/standalone/repository?endpointId=<id>
```

e o redeploy usa a configuração Git da própria stack.

## Acesso ao painel e ao Grafana (SSO)

O painel tem dois modos (`CONTROL_AUTH_MODE`):

- `basic`: usuário/senha de `CONTROL_ADMIN_EMAIL`/`CONTROL_ADMIN_PASSWORD` (HTTP Basic).
- `authentik` (usado no hub desde 19/09/2026): o Traefik aplica o forward auth do Authentik
  (`CONTROL_AUTH_MIDDLEWARE=authentik@docker`), só quem está no grupo `suporte-controle` chega ao
  painel, e o app valida o `X-authentik-jwt` (HS256 com `CONTROL_AUTHENTIK_JWT_SECRET` = client_secret do
  provider `proxy-suporte-controle`). Sem basic auth; "Sair" chama `/outpost.goauthentik.io/sign_out`.
  O router do callback (`/outpost.goauthentik.io/` no host do painel) fica neste compose apontando para
  `CONTROL_AUTH_OUTPOST_SERVICE=authentik@docker`, como o Portainer do hub. Provider/application/grupo
  são criados pelo `scripts/configurar.py` do repo `authentik`.

O Grafana usa OIDC nativo (`GRAFANA_OAUTH_*`, provider `oauth-suporte-grafana`): grupo
`suporte-grafana-admin` = Admin, `suporte-grafana` = Viewer, sem grupo = negado. Com
`GRAFANA_OAUTH_AUTO_LOGIN=true` (hub) abrir o Grafana já leva ao Authentik, sem formulário local; o
`admin` local continua como emergência em `https://grafana.suporte.krato.ai/login?disableAutoLogin=true`.

## Monitoramento

O compose `compose.control-plane.yaml` sobe:

- **Prometheus**: coleta e mantém séries históricas;
- **Grafana**: dashboards detalhados;
- **docker-exporter** (no lugar do cAdvisor): CPU/RAM/rede/reinícios por container/stack;
- **node-exporter**: CPU/RAM/disco/rede da VPS;
- **control**: cadastro/provisionamento e visão resumida;
- **control-db**: cadastro das instalações.

As configurações do Prometheus e o provisionamento do Grafana vão **dentro das imagens**
(`monitoring/prometheus/Dockerfile`, `monitoring/grafana/Dockerfile`), nunca por bind mount relativo:
em stack Git o Portainer clona o repositório dentro do próprio container (`/data/compose/<id>`), esse
caminho não existe no host e o Docker falha ao montar o arquivo. Mudou dashboard ou scrape config →
redeploy da stack (rebuild).

O Prometheus participa da rede do Traefik (`proxy`) além da rede interna: a rede `internal` é
isolada (`internal: true`) e sem a segunda rede ele não alcançaria `https://<cliente>:443`. A coleta
passa pelo Traefik com o certificado real, então `up{job="tenant-app"}` também mede a disponibilidade
externa da instalação. O volume `prom_targets` é preparado pelo serviço `metrics-init` (dono uid 10002
do control, grupo 65534 do Prometheus) — sem isso o control não consegue gravar `tenants.json` nem o
Prometheus ler o token.

O `node-exporter` roda em `network_mode: host` (só assim as interfaces e o tráfego são os da VPS, e não
os da bridge do container) e escuta em `NODE_EXPORTER_PORT` (9101) com basic auth — a VPS não tem
firewall, então a porta ficaria pública sem isso. `NODE_EXPORTER_PASSWORD` vai para o Prometheus e
`NODE_EXPORTER_PASSWORD_HASH_B64` (bcrypt do mesmo valor, em base64 porque o hash tem `$` e o env-file
do Compose/Portainer interpola `$x` dentro dos valores) para o exporter, ambos via `metrics-init`.

**cAdvisor foi substituído** por `monitoring/docker-exporter` (Python, só stdlib, lê o socket do Docker):
com o Docker 29 no snapshotter do containerd (caso do hub) o cAdvisor descarta todos os containers
("failed to identify the read-write layer ID"), mesmo sem a métrica de disco. O exporter publica os mesmos
nomes e labels do cAdvisor (`container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`,
`container_network_*`, `container_start_time_seconds`, `container_last_seen`, `container_label_*`) e ainda
`container_restart_count`, então dashboards e o painel não mudam.

Nesta VPS os pools de endereços padrão do Docker estão esgotados; por isso a rede interna do control
plane tem sub-rede fixa (`CONTROL_SUBNET`, padrão `10.88.0.0/24`) e cada cliente recebe
`TENANT_SUBNET_PREFIX.<1-250>.0/24`. O `node-exporter` ignora interfaces `veth*`/`br-*` (dezenas
delas no host) para a coleta não levar segundos.

Cada aplicação de cliente expõe `/api/ops/metrics`, protegido por Bearer token compartilhado
somente com o Prometheus. Métricas atuais:

- atendimentos ativos, aguardando e convidados;
- salas carregadas;
- WebSockets cliente/atendente;
- conexões dos painéis;
- chamadas WebRTC diretas;
- chamadas WebRTC usando TURN;
- qualidade Boa/Instável/Ruim;
- erros HTTP 5xx e WebSockets recusados (contadores desde o último início do processo);
- instante de início do processo (reinícios aparecem como mudança desse valor).

O dashboard `Suporte Remoto - Operação` no Grafana tem filtro por empresa e três blocos: empresas
(atendimentos, WebSockets, rota direta/TURN, qualidade, disponibilidade, reinícios, erros), recursos por
stack/container (cAdvisor, pelos labels `krato.product`/`krato.tenant` em app, db e redis) e VPS
(CPU, RAM, disco, rede, containers, uptime).

O vídeo e o áudio não são enviados ao monitoramento.

## Teste de capacidade

O monitoramento deve ficar ligado permanentemente. O gerador de carga, não.

Para validar 10, 20 e 30 atendimentos, rode `tools/loadtest/support_load.py` de **outra máquina
ou VPS**, enquanto observa Grafana. O teste sintético cobre API/WebSocket/banco e mantém várias
salas concorrentes; ele não substitui um teste de mídia TURN real.

Exemplo:

```bash
python -m pip install -r tools/loadtest/requirements.txt
python tools/loadtest/support_load.py \
  --base-url https://cliente.suporte.krato.ai \
  --email admin@cliente.com \
  --password 'senha' \
  --sessions 30 \
  --hold 300
```

Durante cinco minutos, observar:

- CPU da aplicação e da VPS;
- RAM;
- conexões simultâneas;
- erros/reconexões;
- latência para abrir as salas;
- rede total da VPS;
- quantidade de chamadas TURN em um teste real com mídia.

Para carga TURN de verdade, use navegadores/celulares ou um runner WebRTC externo forçando
`iceTransportPolicy=relay`. Não execute o gerador pesado na mesma VPS que está sendo medida.

Limites da própria aplicação que afetam o gerador: `POST /api/join` aceita **30 entradas por hora por
IP de origem** (janela alinhada à hora cheia) e `POST /api/invitations` 100 por hora por atendente.
Rodadas de 10 + 20 cabem na mesma hora a partir de um único IP; a de 30 precisa de outra hora ou de
outra máquina/IP. Um `429` no gerador é esse limite, não falha da instalação.

## Primeiro deploy

1. Faça merge da branch quando o CI estiver verde (enquanto isso, as stacks podem apontar
   `SUPPORT_REPO_REF` para a branch de trabalho).
2. Crie o wildcard DNS do domínio padrão (`*.suporte.krato.ai` cobre `controle.`, `grafana.` e os clientes).
3. Gere `CONTROL_MASTER_KEY`:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
4. Copie `.env.control-plane.example` para as variáveis da stack no Portainer.
5. Crie um token de API do Portainer.
6. No Portainer atual, cadastre uma Source Git para este repositório privado e informe o `PORTAINER_SOURCE_ID`.
7. Suba `compose.control-plane.yaml`.
8. Abra `https://<CONTROL_HOST>` e cadastre um cliente de teste.
9. Confirme health, login do cliente, Prometheus e Grafana.
10. Só então migre clientes reais para o provisionamento automático.

## Escala

O produto permanece com **1 worker por instalação**, que é o desenho atual para até cerca de
30 atendimentos simultâneos por empresa. Uma empresa não compartilha o worker com outra:
cada stack possui seu próprio container `app`.

Se no futuro uma única empresa precisar passar desse patamar, a próxima evolução é distribuir
a sinalização WebSocket por Redis Pub/Sub e então permitir múltiplos workers/réplicas.
