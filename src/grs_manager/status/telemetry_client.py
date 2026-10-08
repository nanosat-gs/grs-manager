"""Cliente da API de leitura do GRS Telemetry Decoder.

A telemetria decodificada do satélite, para o operador. Mesmo contrato do
`scheduler_client`: só leitura, por HTTP, e OPCIONAL. Sem TELEMETRY_API_URL,
ou com o decodificador fora — ele só sobe nos profiles de recepção —, a aba de
telemetria mostra "indisponível" e o resto do painel segue igual. O rotor não
pode depender de um serviço de consulta.

O painel não converte nada: a API do decodificador já manda cada campo com o
valor cru, o valor legível e a unidade.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# (conexão, leitura). O painel consulta a cada 15 s e não pode ficar pendurado.
READ_TIMEOUT = (2, 5)


def from_environment() -> Optional["TelemetryApiClient"]:
    """None sem TELEMETRY_API_URL: é como se pede um painel sem telemetria."""
    base_url = os.getenv("TELEMETRY_API_URL")
    if not base_url:
        logger.info("TELEMETRY_API_URL não definida: painel sem a aba de telemetria.")
        return None
    return TelemetryApiClient(base_url)


class TelemetryApiClient:
    def __init__(self, base_url: str, session: Optional[Any] = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session if session is not None else requests.Session()

    def latest(self, profile: str, packet_type: str) -> dict[str, Any]:
        """O pacote mais recente de um tipo.

        :return: {"available": True, "record": {...} ou None} — None quando o
            decodificador está de pé mas ainda não decodificou nenhum; ou
            {"available": False, "error": "..."} quando não deu para perguntar.
        """
        url = (f"{self._base_url}/api/telemetry/{quote(profile, safe='')}/"
               f"{quote(packet_type, safe='')}/latest")
        try:
            response = self._session.get(url, timeout=READ_TIMEOUT)
            response.raise_for_status()
            return {"available": True, "record": response.json().get("record")}
        except (requests.RequestException, ValueError, AttributeError) as error:
            logger.warning("Sem telemetria de %s/%s: %s", profile, packet_type, error)
            return {"available": False, "error": str(error)}

    def close(self) -> None:
        self._session.close()
