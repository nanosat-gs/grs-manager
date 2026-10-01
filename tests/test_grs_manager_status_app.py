import json

import pytest

from grs_manager.domain.models import RotorPosition
from grs_manager.status.app import create_app
from grs_manager.status.scheduler_client import (
    SatelliteNotFound,
    SchedulerRejected,
    SchedulerUnavailable,
)


class FakeStationData:
    """Substitui o acesso ao banco: os testes são sobre o painel, não sobre SQL."""

    def __init__(self, snapshot=None, detail=None, refresh=None):
        self._snapshot = snapshot or {"satellites": [], "database_available": True}
        self._detail = detail
        self._refresh = refresh or {"database_available": True, "results": [], "updated": 0}
        self.refresh_calls = 0

    def snapshot(self):
        return self._snapshot

    def satellite_detail(self, code):
        if self._detail is None or self._detail.get("code") != code:
            return None
        return self._detail

    def refresh_orbital_data(self):
        self.refresh_calls += 1
        return self._refresh

    # Ações do operador: registradas, ou o erro que o teste pedir.
    action_error = None
    actions = None

    def passes(self):
        return {"computed_at": None, "passes": [{"satellite_code": "SAT-001"}],
                "reception": [], "database_available": True}

    def set_reception(self, code, changes):
        return self._act(("reception", code, changes))

    def set_decision(self, code, aos, decision):
        return self._act(("decision", code, aos, decision))

    def set_downlinks(self, code, downlinks):
        return self._act(("downlinks", code, downlinks))

    def _act(self, call):
        if self.actions is None:
            self.actions = []
        self.actions.append(call)
        if self.action_error is not None:
            raise self.action_error
        return {"ok": True}


class FakeStationManagerClient:
    def __init__(self, position):
        self._position = position

    def get_position(self):
        if self._position is None:
            raise RuntimeError("Station Manager indisponível")
        return self._position


# --- Rotor -----------------------------------------------------------------


def test_health_reports_rotor_active_with_position():
    app = create_app(FakeStationManagerClient(RotorPosition(90.0, 20.0)))

    response = app.test_client().get("/health")

    assert response.get_json() == {
        "rotor_connected": True,
        "rotor_position": {"azimuth_degrees": 90.0, "elevation_degrees": 20.0},
    }


def test_health_reports_rotor_inactive_when_station_manager_unreachable():
    app = create_app(FakeStationManagerClient(None))

    response = app.test_client().get("/health")

    assert response.get_json() == {"rotor_connected": False, "rotor_position": None}


def test_status_page_renders_rotor_position():
    app = create_app(FakeStationManagerClient(RotorPosition(123.0, 57.0)))

    body = app.test_client().get("/").get_data(as_text=True)

    assert "123.00" in body
    assert "57.00" in body
    assert "ativo" in body


def test_status_page_shows_placeholder_when_rotor_has_no_reading():
    app = create_app(FakeStationManagerClient(None))

    body = app.test_client().get("/").get_data(as_text=True)

    assert "sem leitura" in body


def test_status_page_subscribes_to_events_stream():
    app = create_app(FakeStationManagerClient(None))

    body = app.test_client().get("/").get_data(as_text=True)

    assert 'new EventSource("/events")' in body


def test_events_stream_sends_current_status_as_first_message():
    app = create_app(FakeStationManagerClient(RotorPosition(10.0, 20.0)))

    # buffered=False mantém o generator preguiçoso -- não espera a resposta
    # inteira (que nunca termina, o stream é infinito) antes de retornar.
    response = app.test_client().get("/events", buffered=False)

    assert response.mimetype == "text/event-stream"
    chunk = next(response.response)
    payload = chunk.decode() if isinstance(chunk, bytes) else chunk
    data = json.loads(payload.removeprefix("data: ").strip())

    assert data == {
        "rotor_connected": True,
        "rotor_position": {"azimuth_degrees": 10.0, "elevation_degrees": 20.0},
    }
    response.close()


def test_panel_does_not_report_rotctld_clients():
    """O rastreamento da estação é próprio: a conexão de um cliente hamlib
    externo não faz mais parte do fluxo normal e saiu do painel."""
    app = create_app(FakeStationManagerClient(RotorPosition(0.0, 0.0)))
    client = app.test_client()

    assert "gpredict" not in client.get("/").get_data(as_text=True).lower()
    assert "gpredict_connected" not in client.get("/health").get_json()


# --- Visão de satélites ----------------------------------------------------


def _app_with_station_data(station_data):
    return create_app(FakeStationManagerClient(RotorPosition(0.0, 0.0)), station_data)


def test_station_endpoint_reports_not_configured_without_database():
    app = create_app(FakeStationManagerClient(None))

    payload = app.test_client().get("/api/station").get_json()

    assert payload == {"satellites": [], "database_available": False, "configured": False}


def test_station_endpoint_returns_satellites():
    snapshot = {
        "satellites": [{"name": "FloripaSat-1", "code": "SAT-001", "elevation_degrees": 12.5}],
        "database_available": True,
    }
    app = _app_with_station_data(FakeStationData(snapshot=snapshot))

    payload = app.test_client().get("/api/station").get_json()

    assert payload["configured"] is True
    assert payload["satellites"][0]["code"] == "SAT-001"


def test_station_endpoint_survives_database_outage():
    """Banco fora do ar é estado a exibir, não erro HTTP: a página precisa
    continuar mostrando o rotor."""
    app = _app_with_station_data(
        FakeStationData(snapshot={"satellites": [], "database_available": False})
    )

    response = app.test_client().get("/api/station")

    assert response.status_code == 200
    assert response.get_json()["database_available"] is False


def test_rotor_health_is_unaffected_by_missing_database():
    app = create_app(FakeStationManagerClient(RotorPosition(1.0, 2.0)))

    assert app.test_client().get("/health").get_json()["rotor_connected"] is True


def test_satellite_detail_returns_payload_for_known_code():
    detail = {"code": "SAT-001", "name": "FloripaSat-1", "norad_id": 44885, "scheduling": []}
    app = _app_with_station_data(FakeStationData(detail=detail))

    payload = app.test_client().get("/api/satellite/SAT-001").get_json()

    assert payload["name"] == "FloripaSat-1"
    assert payload["norad_id"] == 44885


def test_satellite_detail_is_404_for_unknown_code():
    app = _app_with_station_data(FakeStationData(detail={"code": "SAT-001"}))

    assert app.test_client().get("/api/satellite/NOPE").status_code == 404


def test_satellite_detail_is_404_without_database():
    app = create_app(FakeStationManagerClient(None))

    assert app.test_client().get("/api/satellite/SAT-001").status_code == 404


def test_status_page_shows_rotor_alongside_satellite_grid():
    """A visão de satélites é adição, não substituição: o painel de rotor
    continua sendo a razão de existir desta página."""
    app = create_app(FakeStationManagerClient(RotorPosition(123.0, 57.0)))

    body = app.test_client().get("/").get_data(as_text=True)

    assert "123.00" in body
    assert 'id="sat-grid"' in body
    assert 'id="modal"' in body


# --- Revalidação de TLE ----------------------------------------------------


def test_refresh_endpoint_reports_what_was_updated():
    refresh = {
        "database_available": True,
        "updated": 1,
        "results": [
            {"name": "FloripaSat-1", "code": "SAT-001", "norad_id": 44885,
             "status": "updated", "message": None, "source": "omm"},
        ],
    }
    station_data = FakeStationData(refresh=refresh)
    app = _app_with_station_data(station_data)

    payload = app.test_client().post("/api/tle/refresh").get_json()

    assert station_data.refresh_calls == 1
    assert payload["updated"] == 1
    assert payload["results"][0]["status"] == "updated"


def test_refresh_endpoint_is_post_only():
    """Muda estado (o cache de TLE): um GET seria disparado por prefetch."""
    app = _app_with_station_data(FakeStationData())

    assert app.test_client().get("/api/tle/refresh").status_code == 405


def test_refresh_endpoint_is_404_without_database():
    app = create_app(FakeStationManagerClient(None))

    assert app.test_client().post("/api/tle/refresh").status_code == 404


def test_page_offers_the_refresh_button():
    app = create_app(FakeStationManagerClient(RotorPosition(0.0, 0.0)))

    body = app.test_client().get("/").get_data(as_text=True)

    assert 'id="refresh-tle"' in body
    assert '/api/tle/refresh' in body


def test_page_uses_the_station_manager_dashboard_shell():
    """O painel do operador é a interface do Station Manager (identidade e
    layout da Laura), servida por template e alimentada pelos endpoints daqui."""
    app = create_app(FakeStationManagerClient(RotorPosition(0.0, 0.0)))

    body = app.test_client().get("/").get_data(as_text=True)

    assert "<title>SpaceLab Station Manager</title>" in body
    assert 'data-view="satellites"' in body and 'data-view="passes"' in body
    assert 'id="passesView"' in body


def test_page_links_telecommand_editing_to_the_tc_generator():
    """Criar/editar TC continua sendo do TC Generator: o painel só aponta para
    lá (o GRS Manager não escreve no banco)."""
    from grs_manager.status.app import TC_GENERATOR_URL

    app = create_app(FakeStationManagerClient(RotorPosition(0.0, 0.0)))

    body = app.test_client().get("/").get_data(as_text=True)

    assert f'const TC_GENERATOR_URL = "{TC_GENERATOR_URL}"' in body


# --- Previsão e ações do operador --------------------------------------------


def test_passes_endpoint_returns_the_forecast():
    app = _app_with_station_data(FakeStationData())

    payload = app.test_client().get("/api/passes").get_json()

    assert payload["configured"] is True
    assert payload["passes"] == [{"satellite_code": "SAT-001"}]


def test_passes_endpoint_reports_not_configured_without_scheduler():
    app = create_app(FakeStationManagerClient(None))

    response = app.test_client().get("/api/passes")

    assert response.status_code == 200
    assert response.get_json()["configured"] is False


def test_decision_is_forwarded_to_the_scheduler():
    station_data = FakeStationData()
    app = _app_with_station_data(station_data)

    response = app.test_client().put("/api/passes/decision", json={
        "satellite_code": "SAT-001", "aos": "2026-09-29T14:03:00+00:00", "decision": "skip"})

    assert response.status_code == 200
    assert station_data.actions == [("decision", "SAT-001", "2026-09-29T14:03:00+00:00", "skip")]


def test_undo_is_a_null_decision():
    station_data = FakeStationData()
    app = _app_with_station_data(station_data)

    app.test_client().put("/api/passes/decision", json={
        "satellite_code": "SAT-001", "aos": "2026-09-29T14:03:00+00:00", "decision": None})

    assert station_data.actions[0][3] is None


@pytest.mark.parametrize("body", [None, [], {"aos": "x"}, {"satellite_code": "SAT-001"}])
def test_decision_without_pass_identity_is_400(body):
    station_data = FakeStationData()
    app = _app_with_station_data(station_data)

    response = app.test_client().put("/api/passes/decision", json=body)

    assert response.status_code == 400
    assert station_data.actions is None


def test_reception_is_forwarded_to_the_scheduler():
    station_data = FakeStationData()
    app = _app_with_station_data(station_data)

    response = app.test_client().put("/api/satellites/SAT-001/reception",
                                     json={"track_passes": False, "downlink_frequency_hz": 145_900_000})

    assert response.status_code == 200
    assert station_data.actions == [
        ("reception", "SAT-001", {"track_passes": False, "downlink_frequency_hz": 145_900_000})]


@pytest.mark.parametrize(
    "error, status",
    [
        (SchedulerRejected("a frequência vai em Hz, não em MHz"), 400),
        (SatelliteNotFound("satélite NOPE não encontrado"), 404),
        (SchedulerUnavailable("recusou"), 503),
    ],
    ids=["pedido inválido", "satélite inexistente", "scheduler fora"],
)
def test_operator_action_errors_keep_their_meaning(error, status):
    """Os três desfechos são coisas diferentes na tela: corrigir o pedido,
    conferir o código, ou esperar a estação voltar."""
    station_data = FakeStationData()
    station_data.action_error = error
    app = _app_with_station_data(station_data)

    response = app.test_client().put("/api/satellites/SAT-001/reception", json={"track_passes": True})

    assert response.status_code == status
    assert response.get_json()["error"]


def test_rejection_message_reaches_the_operator_verbatim():
    station_data = FakeStationData()
    station_data.action_error = SchedulerRejected("a frequência vai em Hz, não em MHz")
    app = _app_with_station_data(station_data)

    response = app.test_client().put("/api/satellites/SAT-001/reception",
                                     json={"downlink_frequency_hz": 145.9})

    assert response.get_json() == {"error": "a frequência vai em Hz, não em MHz"}


def test_operator_actions_are_503_without_scheduler():
    app = create_app(FakeStationManagerClient(None))
    client = app.test_client()

    assert client.put("/api/passes/decision", json={
        "satellite_code": "SAT-001", "aos": "x", "decision": "skip"}).status_code == 503
    assert client.put("/api/satellites/SAT-001/reception",
                      json={"track_passes": True}).status_code == 503


def test_page_offers_the_forecast_tab():
    app = create_app(FakeStationManagerClient(RotorPosition(0.0, 0.0)))

    body = app.test_client().get("/").get_data(as_text=True)

    assert 'data-view="forecast"' in body and 'id="forecastView"' in body
    assert "/api/passes/decision" in body
    assert "Rastrear passagens para recepção" in body
    assert "/downlinks" in body and "Adicionar downlink" in body


def test_downlinks_are_forwarded_to_the_scheduler():
    station_data = FakeStationData()
    app = _app_with_station_data(station_data)
    lista = [{"name": "beacon", "frequency_hz": 145_900_000, "enabled": True}]

    response = app.test_client().put("/api/satellites/SAT-001/downlinks", json={"downlinks": lista})

    assert response.status_code == 200
    assert station_data.actions == [("downlinks", "SAT-001", lista)]


@pytest.mark.parametrize("body", [None, [], {"downlinks": "beacon"}])
def test_downlinks_without_a_list_is_400(body):
    station_data = FakeStationData()
    app = _app_with_station_data(station_data)

    response = app.test_client().put("/api/satellites/SAT-001/downlinks", json=body)

    assert response.status_code == 400
    assert station_data.actions is None
