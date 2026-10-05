# GRS Manager

Painel do operador e ponte rotctld da estação terrestre do SpaceLab.

Junta as perguntas que o operador faz ao mesmo tempo:

- **"para onde a antena está apontada?"** — perguntando ao Station Manager por
  ZMQ, ao vivo, via Server-Sent Events;
- **"para onde ela deveria estar apontada, e por quê?"** — perguntando o plano
  ao TC Scheduler por HTTP, inclusive as passagens que ficaram de fora;
- **"o que a estação vai ouvir?"** — os downlinks de cada satélite e o erro de
  oscilador que o ajuste fino mediu.

Faz parte da estação terrestre orquestrada em
[nanosat-gs/grs-station](https://github.com/nanosat-gs/grs-station).

## Portas

| Porta | O quê |
|---|---|
| 5590 | Painel do operador (HTTP) |
| 4533 | Ponte rotctld — protocolo hamlib, para clientes tipo gpredict |

A confusão mais comum é abrir a 4533 esperando o painel. Lá está o rotctld, que
fala texto do protocolo hamlib, não HTML.

## O painel

Três abas:

- **Satélites** — os cards dos satélites. O detalhe de cada um tem o bloco
  **Recepção**: liga/desliga o rastreio das passagens sem telecomando e o
  **editor de downlinks** (nome, frequência em MHz com 1 Hz de resolução,
  ligado/desligado, ordem), com o rádio que ouve cada um e o **erro de
  oscilador medido** na última passagem. O botão **Aplicar** corrige a
  frequência no rascunho com esse erro; só o "Salvar" grava. Abaixo, o
  agendamento e o histórico de 24 h do satélite.
- **Passagens** — as próximas passagens escolhidas.
- **Previsão** — todas as passagens das próximas 24 h, escolhidas ou não, com o
  motivo (perdeu o conflito, pulada, recepção desligada). Daqui o operador
  **pula**, **força** ou **desfaz** uma passagem.

O painel não grava nada sozinho: toda ação é repassada à API do TC Scheduler,
que é o único escritor do banco.

### Rotas

| Método | Caminho | O quê |
|---|---|---|
| `GET` | `/` | O painel |
| `GET` | `/health` | Saúde do painel e do Station Manager |
| `GET` | `/events` | Posição do rotor ao vivo (SSE) |
| `GET` | `/api/station`, `/api/satellite/<code>`, `/api/passes` | Repasse da leitura do TC Scheduler |
| `POST` | `/api/tle/refresh` | Revalida os TLEs (pode levar ~1 s por satélite) |
| `PUT` | `/api/passes/decision`, `/api/satellites/<code>/reception`, `/api/satellites/<code>/downlinks` | Repasse das ações do operador |

Sem o TC Scheduler, as leituras respondem 200 com `database_available: false`
e as ações respondem 503 — o painel volta a ser só o controle de rotor.

## Instalar e rodar

```bash
pip install -e ".[dev]"
pytest

python -m grs_manager.main \
  --station-manager-address=tcp://127.0.0.1:5580 \
  --status-port=5590
```

Duas variáveis de ambiente, ambas opcionais (ver `.env.example`):
`TC_SCHEDULER_API_URL` e `TC_GENERATOR_URL`. Sem a primeira o painel funciona
igual, mostrando só o rotor.

## Estrutura

```
src/grs_manager/
├── domain/          # Portas e value objects próprios
├── rotctld/         # Adapter de entrada: bridge TCP compatível com rotctld
├── adapters/        # Adapter de saída: cliente ZMQ do Station Manager
└── status/          # Painel HTTP, o cliente da API do TC Scheduler e o
                     # template da página (templates/index.html)
```

Este serviço não conhece o Postgres — nem para ler. Ver `CLAUDE.md`.
