# GRS Manager

Painel do operador e ponte rotctld da estação terrestre do SpaceLab.

Junta as duas perguntas que o operador faz ao mesmo tempo:

- **"para onde a antena está apontada?"** — perguntando ao Station Manager por
  ZMQ, ao vivo, via Server-Sent Events;
- **"para onde ela deveria estar apontada?"** — perguntando o plano ao TC
  Scheduler por HTTP.

Faz parte da estação terrestre orquestrada em
[nanosat-gs/grs-station](https://github.com/nanosat-gs/grs-station).

## Portas

| Porta | O quê |
|---|---|
| 5590 | Painel do operador (HTTP) |
| 4533 | Ponte rotctld — protocolo hamlib, para clientes tipo gpredict |

A confusão mais comum é abrir a 4533 esperando o painel. Lá está o rotctld, que
fala texto do protocolo hamlib, não HTML.

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
