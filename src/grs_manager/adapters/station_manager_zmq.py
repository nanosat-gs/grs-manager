"""Adapter de saída: fala com o Station Manager (mgm8) via ZMQ REQ/REP.

Implementa RotorControlUseCase traduzindo cada chamada numa mensagem JSON
enviada ao Station Manager, que responde de volta pelo mesmo socket (ZMQ
REQ/REP é síncrono — uma requisição, uma resposta, nessa ordem). Esse é um
protocolo próprio, definido só entre estes dois serviços (ao contrário do
rotctld, que é público/hamlib, ou do Rot2Prog, que é do fabricante do
hardware) — pode evoluir livremente. O schema (espelhado em
`mgm8.rotor_zmq.server`) é:

    {"cmd": "set_target", "azimuth_degrees": <float>, "elevation_degrees": <float>}
        -> {"ok": true, "azimuth_degrees": <float>, "elevation_degrees": <float>}
    {"cmd": "get_position"}
        -> {"ok": true, "azimuth_degrees": <float>, "elevation_degrees": <float>}
    {"cmd": "stop"} / {"cmd": "park"}
        -> {"ok": true}
    qualquer comando, em caso de erro:
        -> {"ok": false, "error": "<mensagem>"}

LIMITAÇÃO CONHECIDA: um socket ZMQ REQ que sofre timeout numa requisição fica
num estado inconsistente (não aceita nova requisição sem antes receber a
resposta pendente). Não há retry/reconexão automática aqui — se o Station
Manager cair no meio de uma chamada, este cliente vai lançar exceção e
precisar ser recriado. Aceitável para uma primeira versão; um padrão mais
resiliente (ex.: "Lazy Pirate") fica para quando for necessário.

Thread-safety: um socket ZMQ REQ não suporta uso concorrente por várias
threads (a máquina de estados send->recv não é protegida internamente). Como
o RotctldServer é multi-thread (uma thread por conexão) e o painel de status
(`grs_manager.status`) também chama este cliente a partir de outra thread,
todo acesso a `_socket` passa por um lock.
"""

from __future__ import annotations

import threading

import zmq

from grs_manager.domain.models import RotorPosition

ZMQ_REQUEST_TIMEOUT_MS = 3000


class StationManagerZmqClient:
    """Implementa RotorControlUseCase enviando comandos ao Station Manager via ZMQ REQ."""

    def __init__(self, station_manager_address: str, timeout_ms: int = ZMQ_REQUEST_TIMEOUT_MS) -> None:
        # Context dedicado (não o Context.instance() compartilhado): evita
        # instabilidade observada no libzmq no Windows quando muitos sockets
        # de contextos/threads diferentes disputam o context global.
        self._context = zmq.Context()
        self._socket = self._context.socket(zmq.REQ)
        self._socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.connect(station_manager_address)
        self._lock = threading.Lock()

    def _request(self, message: dict[str, object]) -> dict[str, object]:
        with self._lock:
            self._socket.send_json(message)
            reply = self._socket.recv_json()
        if not isinstance(reply, dict) or not reply.get("ok", False):
            error = reply.get("error", "resposta inesperada") if isinstance(reply, dict) else repr(reply)
            raise RuntimeError(f"Station Manager retornou erro: {error}")
        return reply

    def set_target(self, azimuth_degrees: float, elevation_degrees: float) -> RotorPosition:
        reply = self._request({
            "cmd": "set_target", "azimuth_degrees": azimuth_degrees, "elevation_degrees": elevation_degrees,
        })
        return RotorPosition(reply["azimuth_degrees"], reply["elevation_degrees"])

    def get_position(self) -> RotorPosition:
        reply = self._request({"cmd": "get_position"})
        return RotorPosition(reply["azimuth_degrees"], reply["elevation_degrees"])

    def stop(self) -> None:
        self._request({"cmd": "stop"})

    def park(self) -> None:
        self._request({"cmd": "park"})

    def close(self) -> None:
        self._socket.close()
        self._context.term()
