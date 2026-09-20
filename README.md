# LEGADO — não usar

> **Este repositório NÃO é a fonte do Control Plane em produção e NÃO deve receber desenvolvimento novo.**

É um **snapshot público** publicado em 19/09/2026 a partir de
`mariltonleal/suporte-remoto` na branch `feat/control-plane-monitoring` no commit `942c535`
(commit local `dfee919`). Serviu só de referência enquanto o frontend era desenhado.

## Use estes repositórios

| O quê | Onde |
|---|---|
| Produto, backend do tenant, backend do Control Plane, compose, Portainer, CI | **https://github.com/mariltonleal/suporte-remoto** |
| Frontend oficial do painel administrativo | **https://github.com/mariltonleal/control-plane-central** |

A **stack Portainer** chamada `suporte-control-plane` clona `mariltonleal/suporte-remoto`
(`compose.control-plane.yaml`). Ela **não** usa este GitHub.

Não abra PRs de feature aqui. Não copie este código para produção.
Licença: todos os direitos reservados (somente leitura/referência histórica).
