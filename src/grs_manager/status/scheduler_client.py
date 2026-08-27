"""Cliente da API de leitura do TC Scheduler.

O painel sempre soube responder "a antena está apontada para onde?". A outra
metade da pergunta do operador — "e ela deveria estar apontada para onde?" —
não está no GRS Manager nem no Station Manager: está no plano que o TC
Scheduler mantém.

Antes este módulo era uma conexão com o Postgres. Passou a ser HTTP para que o
banco tenha um dono só: quem escreve o plano é quem o serve. O GRS Manager é o
único serviço da estação que agora não conhece o Postgres — o mesmo argumento
que já valia para o Station Manager, e pela mesma razão (ele precisa poder
rodar longe do banco).

Os métodos são exatamente os que o painel já consumia (`snapshot`,
`satellite_detail`, `refresh_orbital_data`, `close`), e os dicionários de falha
são idênticos aos que a versão de banco devolvia quando o Postgres caía. É o
que faz `status.app` não precisar saber que a fonte mudou.

Opcional de propósito, como antes. Sem TC_SCHEDULER_API_URL, ou com o Scheduler
parado, o painel volta a ser exatamente o controle de rotor — que é o que o GRS
Manager precisa ser capaz de fazer sozinho. Uma passagem em andamento não pode
parar porque um serviço de consulta caiu.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# (conexão, leitura). O painel busca a cada 15s e não pode ficar pendurado:
# um Scheduler ocupado é indistinguível de um parado, e os dois devem virar
# "sem acesso" na tela em vez de travar a página.
READ_TIMEOUT = (2, 5)

# A revalidação percorre todos os satélites rastreáveis indo ao CelesTrak, um
# a um. Com meia dúzia de satélites e o catálogo lento, ela passa fácil dos
# 30s. Aplicar o timeout de leitura aqui transformaria um refresh que
# FUNCIONOU num erro na tela, e o operador clicaria de novo.
REFRESH_TIMEOUT = (2, 120)


class SchedulerUnavailable(Exception):
    """O Scheduler não respondeu, ou respondeu o que não dá para usar."""


class SatelliteNotFound(Exception):
    """O Scheduler respondeu: esse satélite não existe.

    Separado de `SchedulerUnavailable` de propósito. São coisas diferentes na
    tela — "não encontrado" é uma resposta sobre o satélite, "sem acesso" é uma
    resposta sobre a estação — e tratá-las juntas faria um código digitado
    errado parecer uma falha de infraestrutura.
    """


def from_environment() -> Optional["SchedulerApiClient"]:
    """Constrói a partir de TC_SCHEDULER_API_URL, ou devolve None se não houver.

    None é um resultado legítimo, não um erro: é como se pede um painel só de
    rotor.
    """
    base_url = os.getenv("TC_SCHEDULER_API_URL")
    if not base_url:
        logger.info("TC_SCHEDULER_API_URL não definida: painel sem a visão de satélites.")
        return None
    return SchedulerApiClient(base_url)


class SchedulerApiClient:
    """Acesso somente-leitura ao plano da estação, por HTTP."""

    def __init__(self, base_url: str, session: Optional[Any] = None) -> None:
        self._base_url = base_url.rstrip("/")
        # Session e não requests.get solto: o painel consulta de 15 em 15
        # segundos, e reaproveitar a conexão evita um handshake TCP por ciclo.
        self._session = session if session is not None else requests.Session()

    def snapshot(self) -> dict[str, Any]:
        """Satélites e suas próximas passagens."""
        try:
            return self._request("GET", "/api/station", READ_TIMEOUT)
        except (SchedulerUnavailable, SatelliteNotFound) as error:
            logger.warning("Sem dados da estação: %s", error)
            return {"satellites": [], "database_available": False}

    def satellite_detail(self, code: str) -> Optional[dict[str, Any]]:
        """Tudo o que o modal de um satélite mostra.

        None quando o satélite não existe — o painel transforma isso no seu
        próprio 404. Um erro de conexão devolve o dicionário de indisponível,
        que é o que a página sabe desenhar.
        """
        try:
            return self._request("GET", f"/api/satellite/{quote(code, safe='')}", READ_TIMEOUT)
        except SatelliteNotFound:
            return None
        except SchedulerUnavailable as error:
            logger.warning("Sem detalhe para %s: %s", code, error)
            return {"code": code, "database_available": False}

    def refresh_orbital_data(self) -> dict[str, Any]:
        """Pede ao Scheduler que rebusque os elementos orbitais no CelesTrak.

        Quem escreve o cache de TLE é o Scheduler, que é quem o lê para
        planejar. Antes o painel escrevia num volume compartilhado e torcia
        para os dois concordarem sobre a idade máxima do cache; agora ele
        apenas pede.
        """
        try:
            return self._request("POST", "/api/tle/refresh", REFRESH_TIMEOUT)
        except (SchedulerUnavailable, SatelliteNotFound) as error:
            logger.warning("Falha ao revalidar os TLEs: %s", error)
            return {"database_available": False, "results": []}

    def _request(self, method: str, path: str, timeout: tuple[float, float]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            response = self._session.request(method, url, timeout=timeout)
        except requests.RequestException as error:
            raise SchedulerUnavailable(str(error)) from error

        # Antes do raise_for_status: 404 aqui é uma resposta com significado,
        # e deixá-lo virar HTTPError apagaria a diferença entre "não existe" e
        # "não deu para perguntar".
        if response.status_code == 404:
            raise SatelliteNotFound(url)

        try:
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            raise SchedulerUnavailable(str(error)) from error

    def close(self) -> None:
        self._session.close()
