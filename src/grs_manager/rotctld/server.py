"""Servidor TCP compatível com o protocolo rotctld (hamlib).

O gpredict é cliente: conecta, envia um comando por linha e espera a resposta
antes do próximo. Subconjunto suportado (o que o gpredict de fato usa):

    p            -> lê a posição atual   -> "<az>\\n<el>\\n" (6 casas decimais)
    P <az> <el>  -> comanda o alvo       -> "RPRT 0\\n"
    S            -> para o rotor         -> "RPRT 0\\n"
    \\dump_state  -> capacidades do rotor -> ver DUMP_STATE_RESPONSE (handshake)
    q            -> encerra a conexão    -> (fecha o socket, sem resposta)

O comando \\dump_state é enviado automaticamente pelo backend NET rotctl do
hamlib (usado tanto pelo `rotctl -m 2` quanto pelo gpredict) assim que a
conexão abre, antes de qualquer outro comando — sem essa resposta específica,
o cliente considera a conexão com "Protocol error" e nunca marca como
engajada, mesmo que o socket TCP continue aberto e comandos soltos como "p"
continuem sendo aceitos. Formato confirmado em tests/rotctl_parse.c do
próprio hamlib (função dump_state, caminho não-interativo/não-extended: só
os valores, um por linha, sem RPRT final).

Comando não reconhecido recebe "RPRT 0" genérico, para não travar o gpredict.
Qualquer falha (parsing ou erro vindo do Station Manager, ex.: ZMQ fora do
ar) responde "RPRT -1" em vez de derrubar a conexão.
"""

from __future__ import annotations

import logging
import socketserver
import threading

from grs_manager.domain.ports import RotorControlUseCase

logger = logging.getLogger(__name__)

RPRT_OK = "RPRT 0"
RPRT_ERROR = "RPRT -1"
POSITION_DECIMALS = 6

DUMP_STATE_COMMAND = "\\dump_state"
ROTCTLD_PROTOCOL_VERSION = 1
# ID de modelo placeholder: não corresponde a nenhum backend real do hamlib,
# só precisa ser um inteiro parseável pelo cliente.
ROTCTLD_ROTOR_MODEL = 1
ROTCTLD_SOUTH_ZERO = 0
# Limites de curso divulgados ao gpredict/rotctl no handshake. Não são a
# fonte de verdade — quem de fato aplica o clamp é o TrackingService no
# Station Manager (mgm8.application.tracking_service). Como os dois serviços
# só se falam por ZMQ (sem código compartilhado), esses valores precisam ser
# mantidos em sincronia manualmente com os do outro lado.
AZ_MIN_DEGREES = 0.0
AZ_MAX_DEGREES = 360.0
EL_MIN_DEGREES = 0.0
EL_MAX_DEGREES = 90.0
DUMP_STATE_RESPONSE = "\n".join([
    str(ROTCTLD_PROTOCOL_VERSION),
    str(ROTCTLD_ROTOR_MODEL),
    f"{AZ_MIN_DEGREES:.6f}",
    f"{AZ_MAX_DEGREES:.6f}",
    f"{EL_MIN_DEGREES:.6f}",
    f"{EL_MAX_DEGREES:.6f}",
    str(ROTCTLD_SOUTH_ZERO),
    "rot_type=AzEl",
    "done",
])


class _RotctldRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: RotctldServer = self.server  # type: ignore[assignment]
        server._connection_opened()
        try:
            service: RotorControlUseCase = server.service
            for raw in self.rfile:
                line = raw.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                if line == "q":
                    return
                response = self._dispatch(service, line)
                self.wfile.write((response + "\n").encode("ascii"))
        finally:
            server._connection_closed()

    def _dispatch(self, service: RotorControlUseCase, line: str) -> str:
        parts = line.split()
        command, args = parts[0], parts[1:]
        try:
            if command == "p":
                position = service.get_position()
                return (
                    f"{position.azimuth_degrees:.{POSITION_DECIMALS}f}\n"
                    f"{position.elevation_degrees:.{POSITION_DECIMALS}f}"
                )
            if command == "P":
                azimuth, elevation = float(args[0]), float(args[1])
                service.set_target(azimuth, elevation)
                self.server._record_target_from_gpredict(azimuth, elevation)  # type: ignore[attr-defined]
                return RPRT_OK
            if command == "S":
                service.stop()
                return RPRT_OK
            if command == DUMP_STATE_COMMAND:
                return DUMP_STATE_RESPONSE
        except (IndexError, ValueError):
            logger.warning("Comando rotctld malformado: %r", line)
            return RPRT_ERROR
        except Exception:
            logger.exception("Falha ao executar comando rotctld: %r", line)
            return RPRT_ERROR

        logger.debug("Comando rotctld não tratado, respondendo RPRT 0: %r", line)
        return RPRT_OK


class RotctldServer(socketserver.ThreadingTCPServer):
    """Servidor TCP compatível com rotctld; cada conexão roda na sua própria thread."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int, service: RotorControlUseCase) -> None:
        super().__init__((host, port), _RotctldRequestHandler)
        self.service = service
        self._active_connections = 0
        self._last_target_from_gpredict: tuple[float, float] | None = None
        self._state_lock = threading.Lock()

    @property
    def is_gpredict_connected(self) -> bool:
        """True enquanto pelo menos uma conexão TCP (tipicamente o gpredict) está aberta."""
        with self._state_lock:
            return self._active_connections > 0

    @property
    def last_target_from_gpredict(self) -> tuple[float, float] | None:
        """Último (azimuth, elevation) recebido via comando "P"; None se nenhum ainda."""
        with self._state_lock:
            return self._last_target_from_gpredict

    def _connection_opened(self) -> None:
        with self._state_lock:
            self._active_connections += 1

    def _connection_closed(self) -> None:
        with self._state_lock:
            self._active_connections -= 1

    def _record_target_from_gpredict(self, azimuth_degrees: float, elevation_degrees: float) -> None:
        with self._state_lock:
            self._last_target_from_gpredict = (azimuth_degrees, elevation_degrees)
