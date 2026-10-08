"""A aba de telemetria: cliente da API do grs-telemetry-decoder e a rota do painel.

Mesma propriedade do cliente do Scheduler: com o decodificador fora, o painel
degrada para "indisponível" e não explode. Nada aqui abre socket.
"""

import pytest
import requests

from grs_manager.domain.models import RotorPosition
from grs_manager.status import telemetry_client
from grs_manager.status.app import create_app
from grs_manager.status.telemetry_client import TelemetryApiClient

RECORD = {"raw_packet_id": 7, "received_at": "2026-10-06T12:00:00+00:00", "radio": "uhf",
          "layout": "obdh2-111", "rs_errors": 0, "radio_mismatch": False,
          "fields": [{"name": "obdh_mcu_temp", "raw": 298, "unit": "K", "value": 24.85,
                      "display_unit": "°C", "description": "Temperatura do µC do OBDH"}]}


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
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self._error is not None:
            raise self._error
        return self._response

    def close(self):
        pass


class FakeStationManagerClient:
    def get_position(self):
        return RotorPosition(0.0, 0.0)


# --- cliente ---------------------------------------------------------------------


def test_sem_variavel_nao_ha_cliente(monkeypatch):
    monkeypatch.delenv("TELEMETRY_API_URL", raising=False)

    assert telemetry_client.from_environment() is None


def test_com_variavel_ha_cliente(monkeypatch):
    monkeypatch.setenv("TELEMETRY_API_URL", "http://decoder:5592/")

    assert isinstance(telemetry_client.from_environment(), TelemetryApiClient)


def test_latest_pede_a_rota_do_tipo_com_timeout():
    session = FakeSession(FakeResponse(payload={"record": RECORD}))

    got = TelemetryApiClient("http://decoder:5592/", session).latest("fs2", "general_telemetry")

    assert got == {"available": True, "record": RECORD}
    assert session.calls == [("http://decoder:5592/api/telemetry/fs2/general_telemetry/latest",
                              telemetry_client.READ_TIMEOUT)]


def test_decodificador_de_pe_sem_telemetria_ainda():
    session = FakeSession(FakeResponse(payload={"record": None}))

    assert TelemetryApiClient("http://d", session).latest("fs2", "general_telemetry") == {
        "available": True, "record": None}


@pytest.mark.parametrize("session", [
    FakeSession(error=requests.ConnectionError("recusado")),
    FakeSession(error=requests.Timeout("lento")),
    FakeSession(FakeResponse(status_code=503)),
    FakeSession(FakeResponse(invalid_json=True)),
])
def test_qualquer_falha_vira_indisponivel(session):
    got = TelemetryApiClient("http://d", session).latest("fs2", "general_telemetry")

    assert got["available"] is False
    assert got["error"]


# --- rota do painel ----------------------------------------------------------------


class FakeTelemetry:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def latest(self, profile, packet_type):
        self.calls.append((profile, packet_type))
        return self.result


def test_rota_sem_decodificador_configurado():
    app = create_app(FakeStationManagerClient())

    assert app.test_client().get("/api/telemetry/latest").get_json() == {
        "configured": False, "available": False, "record": None}


def test_rota_repassa_o_general_telemetry_do_fs2():
    telemetry = FakeTelemetry({"available": True, "record": RECORD})
    app = create_app(FakeStationManagerClient(), None, telemetry)

    body = app.test_client().get("/api/telemetry/latest").get_json()

    assert body == {"available": True, "record": RECORD, "configured": True}
    assert telemetry.calls == [("fs2", "general_telemetry")]


def test_rota_com_decodificador_fora_responde_200():
    """Indisponível é estado da tela, não erro HTTP: a página desenha o aviso."""
    app = create_app(FakeStationManagerClient(), None, FakeTelemetry({"available": False, "error": "x"}))

    response = app.test_client().get("/api/telemetry/latest")

    assert response.status_code == 200
    assert response.get_json()["available"] is False


def test_pagina_tem_a_aba_de_telemetria():
    html = create_app(FakeStationManagerClient()).test_client().get("/").get_data(as_text=True)

    assert 'data-view="telemetry"' in html
    assert 'id="telemetryView"' in html
    assert "/api/telemetry/latest" in html
