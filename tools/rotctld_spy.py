"""Ferramenta de diagnóstico: aceita a conexão do gpredict e imprime os
comandos rotctld crus recebidos, respondendo sempre RPRT 0 (ou uma posição
fixa para 'p'), para inspecionar exatamente o que o cliente envia.
"""

from __future__ import annotations

import argparse
import socketserver

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4533


class _SpyHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        print(f"Conexão aberta: {self.client_address}")
        for raw in self.rfile:
            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue
            print(f"<- {line!r}")
            if line == "q":
                break
            if line == "p":
                self.wfile.write(b"0.000000\n0.000000\n")
            else:
                self.wfile.write(b"RPRT 0\n")
        print(f"Conexão fechada: {self.client_address}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Espiona comandos rotctld enviados pelo gpredict")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    with socketserver.ThreadingTCPServer((args.host, args.port), _SpyHandler) as server:
        server.allow_reuse_address = True
        print(f"Escutando em {args.host}:{args.port} (Ctrl+C para sair)")
        server.serve_forever()


if __name__ == "__main__":
    main()
