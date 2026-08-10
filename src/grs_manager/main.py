"""Composition root do GRS Manager: único módulo que conhece implementações concretas.

Liga o adapter de entrada (rotctld, fala com o gpredict/Satellite Tracker) ao
adapter de saída (ZMQ, fala com o Station Manager), e opcionalmente sobe o
painel de status (Flask) numa thread separada. Nenhuma dessas peças conhece
as outras diretamente — só este módulo as amarra.
"""

from __future__ import annotations

import argparse
import logging
import threading

from grs_manager.adapters.station_manager_zmq import StationManagerZmqClient
from grs_manager.rotctld.server import RotctldServer
from grs_manager.status.app import create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4533
DEFAULT_STATION_MANAGER_ADDRESS = "tcp://127.0.0.1:5580"
DEFAULT_STATUS_HOST = "127.0.0.1"
DEFAULT_STATUS_PORT = 5590
# Timeout curto pro cliente ZMQ dedicado do painel: uma checagem de saúde não
# deve travar a página por 3s (o timeout padrão do cliente "de produção").
STATUS_ZMQ_TIMEOUT_MS = 1000


def main() -> None:
    parser = argparse.ArgumentParser(description="GRS Manager: ponte rotctld (gpredict) <-> Station Manager (ZMQ)")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Endereço onde o servidor rotctld escuta")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Porta rotctld (padrão hamlib: 4533)")
    parser.add_argument("--station-manager-address", default=DEFAULT_STATION_MANAGER_ADDRESS,
                         help="Endereço ZMQ (REQ) do Station Manager")
    parser.add_argument("--status-host", default=DEFAULT_STATUS_HOST, help="Endereço do painel de status HTTP")
    parser.add_argument("--status-port", type=int, default=DEFAULT_STATUS_PORT, help="Porta do painel de status HTTP")
    parser.add_argument("--no-status", action="store_true", help="Não sobe o painel de status HTTP")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger(__name__)

    service = StationManagerZmqClient(args.station_manager_address)
    server = RotctldServer(args.host, args.port, service)

    status_client = None
    if not args.no_status:
        status_client = StationManagerZmqClient(args.station_manager_address, timeout_ms=STATUS_ZMQ_TIMEOUT_MS)
        status_app = create_app(server, status_client)
        status_thread = threading.Thread(
            target=status_app.run,
            kwargs={"host": args.status_host, "port": args.status_port, "debug": False, "use_reloader": False},
            daemon=True,
        )
        status_thread.start()
        logger.info("Painel de status em http://%s:%d", args.status_host, args.status_port)

    logger.info("GRS Manager servindo rotctld em %s:%d -> Station Manager em %s",
                args.host, args.port, args.station_manager_address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Encerrando por interrupção do usuário.")
    finally:
        server.shutdown()
        server.server_close()
        service.close()
        if status_client is not None:
            status_client.close()


if __name__ == "__main__":
    main()
