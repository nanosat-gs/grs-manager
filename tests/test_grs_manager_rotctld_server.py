import socket
import threading
import time

import pytest

from grs_manager.domain.models import RotorPosition
from grs_manager.rotctld.server import RotctldServer


class FakeStationManager:
    """Duplo de RotorControlUseCase -- imita o Station Manager sem precisar de ZMQ."""

    def __init__(self):
        self.position = RotorPosition(0.0, 0.0)
        self.stopped = False
        self.parked = False

    def set_target(self, azimuth_degrees, elevation_degrees):
        self.position = RotorPosition(azimuth_degrees, elevation_degrees)
        return self.position

    def get_position(self):
        return self.position

    def stop(self):
        self.stopped = True

    def park(self):
        self.parked = True


class RaisingStationManager(FakeStationManager):
    def set_target(self, azimuth_degrees, elevation_degrees):
        raise RuntimeError("Station Manager indisponível")


def start_server(service):
    server = RotctldServer("127.0.0.1", 0, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def server():
    instance = start_server(FakeStationManager())
    yield instance
    instance.shutdown()
    instance.server_close()


def send_lines(address, *lines):
    with socket.create_connection(address, timeout=2) as sock:
        sock.sendall(("\n".join(lines) + "\n").encode("ascii"))
        sock.settimeout(2)
        chunks = []
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        except TimeoutError:
            pass
    return b"".join(chunks).decode("ascii").splitlines()


def test_p_command_returns_current_position(server):
    responses = send_lines(server.server_address, "p", "q")

    assert responses == ["0.000000", "0.000000"]


def test_dump_state_command_returns_rotor_capabilities(server):
    responses = send_lines(server.server_address, "\\dump_state", "q")

    assert responses == [
        "1",
        "1",
        "0.000000",
        "360.000000",
        "0.000000",
        "90.000000",
        "0",
        "rot_type=AzEl",
        "done",
    ]


def test_uppercase_p_command_sets_target_and_acks(server):
    responses = send_lines(server.server_address, "P 180.5 45.25", "p", "q")

    assert responses[0] == "RPRT 0"
    assert responses[1:] == ["180.500000", "45.250000"]


def test_stop_command_acks(server):
    responses = send_lines(server.server_address, "S", "q")

    assert responses == ["RPRT 0"]


def test_is_gpredict_connected_reflects_active_tcp_connection(server):
    assert server.is_gpredict_connected is False

    with socket.create_connection(server.server_address, timeout=2) as sock:
        sock.sendall(b"p\n")
        sock.recv(4096)
        assert server.is_gpredict_connected is True

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and server.is_gpredict_connected:
        time.sleep(0.01)
    assert server.is_gpredict_connected is False


def test_unknown_command_gets_generic_ack_instead_of_dropping_connection(server):
    responses = send_lines(server.server_address, "X", "p", "q")

    assert responses[0] == "RPRT 0"
    assert responses[1:] == ["0.000000", "0.000000"]


def test_malformed_p_command_returns_error_without_dropping_connection(server):
    responses = send_lines(server.server_address, "P not-a-number", "p", "q")

    assert responses[0] == "RPRT -1"
    assert responses[1:] == ["0.000000", "0.000000"]


def test_service_exception_returns_error_instead_of_closing_connection():
    server = start_server(RaisingStationManager())
    try:
        responses = send_lines(server.server_address, "P 10 10", "q")
    finally:
        server.shutdown()
        server.server_close()

    assert responses == ["RPRT -1"]
