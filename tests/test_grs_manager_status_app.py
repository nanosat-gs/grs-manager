from grs_manager.domain.models import RotorPosition
from grs_manager.status.app import create_app


class FakeRotctldServer:
    def __init__(self, connected, last_target=None):
        self.is_gpredict_connected = connected
        self.last_target_from_gpredict = last_target


class FakeStationManagerClient:
    def __init__(self, position):
        self._position = position

    def get_position(self):
        if self._position is None:
            raise RuntimeError("Station Manager indisponível")
        return self._position


def test_health_reports_both_active_with_values():
    server = FakeRotctldServer(True, last_target=(90.0, 20.0))
    client = FakeStationManagerClient(RotorPosition(90.0, 20.0))
    app = create_app(server, client)

    response = app.test_client().get("/health")

    assert response.get_json() == {
        "gpredict_connected": True,
        "gpredict_last_target": {"azimuth_degrees": 90.0, "elevation_degrees": 20.0},
        "rotor_connected": True,
        "rotor_position": {"azimuth_degrees": 90.0, "elevation_degrees": 20.0},
    }


def test_health_reports_rotor_inactive_when_station_manager_unreachable():
    server = FakeRotctldServer(True)
    client = FakeStationManagerClient(None)
    app = create_app(server, client)

    response = app.test_client().get("/health")

    assert response.get_json() == {
        "gpredict_connected": True,
        "gpredict_last_target": None,
        "rotor_connected": False,
        "rotor_position": None,
    }


def test_health_reports_gpredict_disconnected_with_no_target_yet():
    server = FakeRotctldServer(False)
    client = FakeStationManagerClient(RotorPosition(0.0, 0.0))
    app = create_app(server, client)

    response = app.test_client().get("/health")

    assert response.get_json()["gpredict_connected"] is False
    assert response.get_json()["gpredict_last_target"] is None


def test_status_page_renders_values():
    server = FakeRotctldServer(True, last_target=(123.4, 56.7))
    client = FakeStationManagerClient(RotorPosition(123.0, 57.0))
    app = create_app(server, client)

    body = app.test_client().get("/").get_data(as_text=True)

    assert "123.40" in body
    assert "56.70" in body
    assert "123.00" in body
    assert "57.00" in body
    assert "ativo" in body


def test_status_page_shows_placeholder_when_no_target_yet():
    server = FakeRotctldServer(False)
    client = FakeStationManagerClient(None)
    app = create_app(server, client)

    body = app.test_client().get("/").get_data(as_text=True)

    assert "nenhum comando recebido ainda" in body
    assert "sem leitura" in body
