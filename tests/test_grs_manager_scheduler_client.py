"""Testes do cliente da API de leitura, no lado do GRS Manager.

A propriedade que estes testes protegem é a que o CLAUDE.md chama de "degrada
em vez de falhar": com o TC Scheduler parado, o painel tem que continuar de pé
mostrando o rotor, e não explodir. Isso só funciona se o cliente devolver os
mesmos dicionários que a versão de banco devolvia quando o Postgres caía — daí
os testes compararem formatos, e não só ausência de exceção.

Nada aqui abre socket: o dublê de Session responde no lugar do HTTP.
"""

import json

import pytest

import requests

from grs_manager.status import scheduler_client
from grs_manager.status.scheduler_client import SchedulerApiClient


class FakeResponse:
    def __init__(self, status_code=200, payload=None, invalid_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self._invalid_json = invalid_json

    def json(self):
        if self._invalid_json:
            raise ValueError("corpo não é JSON")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Registra o que foi pedido e devolve o que o teste mandar."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []
        self.closed = False

    def request(self, method, url, timeout=None):
        self.calls.append({"method": method, "url": url, "timeout": timeout})
        if self._error is not None:
            raise self._error
        return self._response

    def close(self):
        self.closed = True


def _client(session, base_url="http://tc-scheduler:5591"):
    return SchedulerApiClient(base_url, session=session)


# --- from_environment ------------------------------------------------------


def test_sem_variavel_nao_ha_cliente(monkeypatch):
    """None não é erro: é como se pede um painel só de rotor."""
    monkeypatch.delenv("TC_SCHEDULER_API_URL", raising=False)

    assert scheduler_client.from_environment() is None


def test_com_variavel_constroi_o_cliente(monkeypatch):
    monkeypatch.setenv("TC_SCHEDULER_API_URL", "http://tc-scheduler:5591/")

    client = scheduler_client.from_environment()

    assert isinstance(client, SchedulerApiClient)
    # A barra final não pode virar "//api/station" na URL montada.
    assert client._base_url == "http://tc-scheduler:5591"


# --- snapshot --------------------------------------------------------------


def test_snapshot_repassa_o_que_a_api_devolveu():
    payload = {"satellites": [{"code": "SAT-001"}], "database_available": True}
    session = FakeSession(FakeResponse(payload=payload))

    assert _client(session).snapshot() == payload
    assert session.calls[0]["url"] == "http://tc-scheduler:5591/api/station"
    assert session.calls[0]["timeout"] == scheduler_client.READ_TIMEOUT


@pytest.mark.parametrize(
    "session",
    [
        FakeSession(error=requests.ConnectionError("recusou")),
        FakeSession(error=requests.Timeout("demorou")),
        FakeSession(FakeResponse(status_code=500)),
        FakeSession(FakeResponse(invalid_json=True)),
    ],
    ids=["conexão recusada", "timeout", "erro 500", "corpo inválido"],
)
def test_snapshot_degrada_no_mesmo_formato_de_sempre(session):
    """Formato idêntico ao que station_data devolvia com o banco fora do ar:
    é o que faz o JS da página não precisar saber que a fonte mudou."""
    assert _client(session).snapshot() == {"satellites": [], "database_available": False}


# --- satellite_detail ------------------------------------------------------


def test_detalhe_repassa_o_que_a_api_devolveu():
    detail = {"code": "SAT-001", "name": "Alfa", "database_available": True}
    session = FakeSession(FakeResponse(payload=detail))

    assert _client(session).satellite_detail("SAT-001") == detail


def test_detalhe_de_satelite_inexistente_e_none():
    """404 é resposta, não falha: o painel transforma isto no próprio 404."""
    session = FakeSession(FakeResponse(status_code=404, payload={"error": "não encontrado"}))

    assert _client(session).satellite_detail("NAO-EXISTE") is None


def test_detalhe_com_scheduler_fora_nao_e_confundido_com_inexistente():
    """A distinção que mais importa do arquivo. Se uma falha de conexão
    virasse None, o painel diria "satélite não encontrado" para um satélite
    que existe — e o operador iria procurar o problema no lugar errado."""
    session = FakeSession(error=requests.ConnectionError("recusou"))

    assert _client(session).satellite_detail("SAT-001") == {
        "code": "SAT-001", "database_available": False
    }


def test_codigo_com_caractere_especial_e_escapado():
    """Um código com barra não pode virar outro caminho na URL."""
    session = FakeSession(FakeResponse(payload={"code": "A/B"}))

    _client(session).satellite_detail("A/B")

    assert session.calls[0]["url"].endswith("/api/satellite/A%2FB")


# --- refresh_orbital_data --------------------------------------------------


def test_refresh_usa_post_e_o_timeout_longo():
    """O timeout de leitura curto aqui transformaria um refresh que funcionou
    num erro na tela: a revalidação vai ao CelesTrak uma vez por satélite."""
    session = FakeSession(FakeResponse(payload={"database_available": True, "results": [], "updated": 2}))

    result = _client(session).refresh_orbital_data()

    assert result["updated"] == 2
    assert session.calls[0]["method"] == "POST"
    assert session.calls[0]["timeout"] == scheduler_client.REFRESH_TIMEOUT
    assert scheduler_client.REFRESH_TIMEOUT[1] > scheduler_client.READ_TIMEOUT[1]


def test_refresh_degrada_no_formato_de_sempre():
    session = FakeSession(error=requests.Timeout("demorou"))

    assert _client(session).refresh_orbital_data() == {
        "database_available": False, "results": []
    }


# --- ciclo de vida ---------------------------------------------------------


def test_close_fecha_a_sessao():
    session = FakeSession(FakeResponse())

    _client(session).close()

    assert session.closed


def test_respostas_de_falha_sao_serializaveis():
    """O painel devolve estes dicionários direto no jsonify."""
    session = FakeSession(error=requests.ConnectionError("recusou"))
    client = _client(session)

    for payload in (client.snapshot(), client.satellite_detail("X"), client.refresh_orbital_data()):
        json.dumps(payload)
