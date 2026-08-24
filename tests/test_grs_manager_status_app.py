import json

from grs_manager.domain.models import RotorPosition
from grs_manager.status.app import create_app


class FakeStationData:
    """Substitui o acesso ao banco: os testes são sobre o painel, não sobre SQL."""

    def __init__(self, snapshot=None, detail=None):
        self._snapshot = snapshot or {"satellites": [], "database_available": True}
        self._detail = detail

    def snapshot(self):
        return self._snapshot

    def satellite_detail(self, code):
        if self._detail is None or self._detail.get("code") != code:
            return None
        return self._detail


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
