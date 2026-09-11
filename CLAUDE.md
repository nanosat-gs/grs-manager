# GRS Manager (`grs_manager`)

**Este repo é o GRS Manager, não o Station Manager.** Os nomes são parecidos e
os serviços são diferentes:

- **GRS Manager** (aqui): painel do operador e ponte rotctld. É o Control
  Desktop. Não move rotor — *pede* que movam.
- **Station Manager** ([nanosat-gs/grs-station-manager](https://github.com/nanosat-gs/grs-station-manager)):
  quem de fato controla o rotor. Este serviço é cliente dele.

## Portas

| Porta | Protocolo | Direção |
|---|---|---|
| 4533 | TCP rotctld (hamlib) | **expõe** — para clientes tipo gpredict |
| 5590 | HTTP (Flask + SSE) | **expõe** — o painel do operador |
| 5580 | ZMQ REQ | **consome** — Station Manager |
| 5591 | HTTP | **consome** — API de leitura do TC Scheduler |

## O serviço que não conhece o banco

Este é o único serviço da estação sem nenhuma dependência de Postgres. Ele já
não escrevia; agora também não lê. O plano da estação vem por HTTP do TC
Scheduler, que é quem o escreve.

Isso é o mesmo argumento que já valia para o Station Manager ("ele precisa
poder rodar longe do banco"), agora estendido ao Control Desktop. E o resultado
aparece no `pyproject.toml`: três dependências, nenhuma git URL, e o Dockerfile
mais curto do conjunto.

## Degradar em vez de falhar

**A propriedade mais importante deste repo.** O GRS Manager é a ponte do rotor,
e uma passagem em andamento não pode parar porque um serviço de consulta caiu.

`scheduler_client.py` devolve, em toda falha, os **mesmos dicionários** que a
versão antiga (que abria o Postgres) devolvia com o banco fora do ar. É o que
faz o JS da página não precisar saber que a fonte mudou:

| Situação | O que acontece |
|---|---|
| Sem `TC_SCHEDULER_API_URL` | `from_environment()` devolve `None`; painel só de rotor |
| Scheduler parado / timeout / 5xx | `{"satellites": [], "database_available": false}` |
| HTTP 404 | `satellite_detail` devolve `None` — o painel faz o próprio 404 |

**404 é traduzido antes do `raise_for_status()`**, de propósito. "Esse satélite
não existe" e "não deu para perguntar" são coisas diferentes na tela; confundi-
las faria um código digitado errado parecer falha de infraestrutura, e o
operador iria procurar o problema no lugar errado.

`rotor_position()` não passa pelo cliente HTTP em nenhum caminho. Com o TC
Scheduler inteiro parado, `/health`, `/events` e a metade de rotor da página
continuam funcionando.

## Armadilhas

- **Dois timeouts, não um.** `READ_TIMEOUT` é (2, 5); `REFRESH_TIMEOUT` é
  (2, 120). A revalidação de TLE vai ao CelesTrak **uma vez por satélite**
  (medido: ~1s cada), então o timeout curto transformaria um refresh que
  funcionou num erro na tela — e o operador clicaria de novo.
- **Dois clientes ZMQ para o mesmo endereço.** Um de produção (3000 ms) e um
  dedicado ao painel (1000 ms). Um socket REQ que deu timeout fica num estado
  inconsistente, então uma checagem de saúde não pode compartilhar o socket do
  tráfego real de rotor.
- **O painel não reporta o rotctld.** Ele continua de pé na 4533 para quem
  quiser assumir a antena por um cliente hamlib, mas o rastreamento da estação
  é próprio — destacar essa conexão sugeriria que ela ainda faz parte do fluxo
  normal, o que deixou de ser verdade.
- **Limites de curso duplicados.** `AZ_MIN/AZ_MAX/EL_MIN/EL_MAX` existem aqui
  (`rotctld/server.py`, só para anunciar no `\dump_state`) e no Station Manager
  (que de fato aplica o clamp). São repositórios diferentes agora; divergir é
  possível, e o efeito seria o gpredict acreditar num curso que a antena não
  tem.
- **A página é um template Jinja** (`status/templates/index.html`), com a
  identidade do dashboard do Station Manager. As abas são `.panel-view`, que
  nascem `display: none` e só aparecem com o `.active` que o JS atribui; `.panel`
  é só o estilo de card. O template entra no wheel pelo `package-data` do
  pyproject — sem ele o painel só funcionaria em instalação editável.

## Rodar

```bash
pip install -e ".[dev]"
pytest

python -m grs_manager.main --station-manager-address=tcp://127.0.0.1:5580
```

O painel sobe em `http://127.0.0.1:5590` (`--no-status` desliga). A confusão
mais comum é abrir a 4533 esperando o painel — lá está o rotctld, protocolo
hamlib, que responde em texto e não em HTML.
