"""Leitura do plano da estação, para o painel do GRS Manager.

O painel sempre soube responder "a antena está apontada para onde?". O que
faltava era a outra metade da pergunta do operador: "e ela deveria estar
apontada para onde?". Isso não está no GRS Manager nem no Station Manager —
está no banco, escrito pelo TC Scheduler (posição corrente de cada satélite e
o plano de passagens).

Somente leitura, e deliberadamente opcional. O GRS Manager é a ponte do rotor:
ele precisa continuar de pé mesmo com o Postgres fora do ar, porque uma
passagem em andamento não pode parar por causa do banco. Sem PG_DATABASE_URL,
ou com o banco inacessível, o painel volta a ser exatamente o que era antes —
rotor e gpredict — em vez de falhar por inteiro.

Não escreve nada: quem escreve é o TC Scheduler, e essa continua sendo a
regra. Aqui é só consulta.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Um TLE novo chega a cada poucas horas; reconstruir o Satrec a cada abertura
# de modal só renderia uma ida ao CelesTrak por clique. O cache é por processo
# e some no restart, o que é aceitável para dado que se busca de novo.
SATREC_TTL_SECONDS = 6 * 3600

# Quanto do passado a aba de histórico do modal cobre.
HISTORY_WINDOW = timedelta(hours=24)

# Telecomando que ainda não saiu. 'queued' é o que o Scheduler marca ao
# comprometer uma passagem; 'pending' é o que ainda não tem hora marcada.
PENDING_STATUSES = ("pending", "queued")

# Uma linha por satélite cadastrado, com a posição corrente quando houver.
# LEFT JOIN e não INNER: um satélite sem dados orbitais precisa aparecer no
# painel como "não rastreável", e não sumir dele.
_SATELLITES_SQL = """
    SELECT s.id, s.name, s.code, s.status, s.norad_id,
           (s.norad_id IS NOT NULL
            OR (s.tle_line1 IS NOT NULL AND s.tle_line2 IS NOT NULL)) AS is_trackable,
           ts.azimuth_deg, ts.elevation_deg, ts.range_km, ts.is_visible,
           ts.status_message, ts.checked_at
    FROM satellites s
    LEFT JOIN satellite_tracking_status ts ON ts.satellite_id = s.id
    ORDER BY s.name
"""

# Só o que ainda vai acontecer: passagem cujo LOS já passou não é plano, é
# histórico, e a grade é sobre o agora. Ordenado por AOS para que a primeira
# linha de cada satélite seja a próxima passagem dele.
_PASSES_SQL = """
    SELECT p.id, p.satellite_id, p.aos_time, p.los_time, p.max_elevation_deg, p.status,
           (SELECT COUNT(*) FROM telecommands t WHERE t.scheduled_pass_id = p.id)
               AS telecommand_count
    FROM scheduled_passes p
    WHERE p.status IN ('planned', 'active') AND p.los_time >= now()
    ORDER BY p.aos_time
"""

# Só quem tem NORAD ID entra na revalidação: um satélite de TLE manual não tem
# o que buscar no catálogo, e um sem dado orbital nenhum não é rastreável.
_TRACKABLE_SQL = """
    SELECT s.id, s.name, s.code, s.norad_id,
           (s.tle_line1 IS NOT NULL AND s.tle_line2 IS NOT NULL) AS has_manual_tle
    FROM satellites s
    WHERE s.norad_id IS NOT NULL
       OR (s.tle_line1 IS NOT NULL AND s.tle_line2 IS NOT NULL)
    ORDER BY s.name
"""

_SATELLITE_BY_CODE_SQL = """
    SELECT s.id, s.name, s.code, s.status, s.norad_id, s.tle_line1, s.tle_line2,
           ts.azimuth_deg, ts.elevation_deg, ts.range_km, ts.is_visible,
           ts.status_message, ts.checked_at
    FROM satellites s
    LEFT JOIN satellite_tracking_status ts ON ts.satellite_id = s.id
    WHERE s.code = :code
"""

# Futuro e passado recente na mesma consulta: são as duas abas do modal
# ("Scheduling" e "History"), e separá-las em duas idas ao banco só daria a
# chance de uma passagem trocar de estado entre elas.
_SATELLITE_PASSES_SQL = """
    SELECT p.id, p.aos_time, p.los_time, p.culmination_time,
           p.max_elevation_deg, p.status, p.status_message
    FROM scheduled_passes p
    WHERE p.satellite_id = :satellite_id
      AND p.los_time >= :history_since
    ORDER BY p.aos_time
"""

# Os telecomandos de todas as passagens do satélite de uma vez. Prioridade
# primeiro porque é a ordem em que o Scheduler os considera.
_TELECOMMANDS_SQL = """
    SELECT t.id, t.scheduled_pass_id, t.command_type, t.status, t.priority
    FROM telecommands t
    WHERE t.satellite_id = :satellite_id
    ORDER BY t.priority DESC, t.id
"""


def from_environment() -> Optional[StationData]:
    """Constrói a partir de PG_DATABASE_URL, ou devolve None se não houver.

    None é um resultado legítimo, não um erro: é como se pede um painel só de
    rotor (o comportamento anterior a esta funcionalidade).
    """
    database_url = os.getenv("PG_DATABASE_URL")
    if not database_url:
        logger.info("PG_DATABASE_URL não definida: painel sem a visão de satélites.")
        return None
    return StationData(database_url)


class StationData:
    """Acesso somente-leitura ao plano da estação."""

    def __init__(self, database_url: str, station: Optional[dict] = None) -> None:
        # Import tardio: SQLAlchemy só é necessário quando o painel de fato vai
        # ler o banco. Assim o GRS Manager continua importável (e testável) em
        # ambientes que instalaram só o essencial, sem o extra `scheduler`.
        from sqlalchemy import create_engine

        # pool_pre_ping porque este processo vive por dias: sem ele, a primeira
        # consulta depois de um restart do Postgres falharia com uma conexão
        # morta reaproveitada do pool.
        self._engine = create_engine(database_url, pool_pre_ping=True, pool_size=2, max_overflow=0)
        self._station = station
        self._satrec_cache: dict[str, tuple[Any, float]] = {}
        self._cache_lock = threading.Lock()

    @property
    def station(self) -> dict:
        """Coordenadas da estação, as mesmas que Scheduler e Station Manager usam."""
        if self._station is None:
            from spacelab_tracking import config as tracking_config

            self._station = tracking_config.GROUND_STATION
        return self._station

    # --- Grade --------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Satélites e suas próximas passagens, num retrato consistente.

        As duas consultas vão na mesma transação de propósito: ler em partes
        daria a posição de um instante com o plano de outro, justamente quando
        algo está mudando.

        Uma falha aqui devolve `database_available: False`, e não uma exceção:
        banco fora do ar é um estado a mostrar na tela, não um motivo para
        derrubar o painel do rotor junto.
        """
        from sqlalchemy import text

        try:
            with self._engine.connect() as conn:
                satellites = [dict(row) for row in conn.execute(text(_SATELLITES_SQL)).mappings()]
                passes = [dict(row) for row in conn.execute(text(_PASSES_SQL)).mappings()]
        except Exception as error:
            logger.warning("Sem dados da estação: %s", error)
            return {"satellites": [], "database_available": False}

        next_pass_by_satellite: dict[int, dict[str, Any]] = {}
        for scheduled in passes:
            next_pass_by_satellite.setdefault(scheduled["satellite_id"], scheduled)

        return {
            "satellites": [
                _satellite_to_dict(satellite, next_pass_by_satellite.get(satellite["id"]))
                for satellite in satellites
            ],
            "database_available": True,
        }

    # --- Detalhe de um satélite ---------------------------------------------

    def satellite_detail(self, code: str) -> Optional[dict[str, Any]]:
        """Tudo o que o modal de um satélite mostra, ou None se ele não existe.

        A posição é recalculada aqui em vez de lida do banco: o modal mostra o
        vetor de estado (TEME) e o ponto subsatélite, que o Scheduler não grava
        — ele só persiste o resumo que a grade usa. Propagar custa
        microssegundos, então vale mais recalcular do que alargar a tabela de
        estado corrente com colunas que só uma tela consome.
        """
        from sqlalchemy import text

        history_since = _now() - HISTORY_WINDOW
        try:
            with self._engine.connect() as conn:
                row = conn.execute(text(_SATELLITE_BY_CODE_SQL), {"code": code}).mappings().first()
                if row is None:
                    return None
                satellite = dict(row)
                passes = [dict(p) for p in conn.execute(
                    text(_SATELLITE_PASSES_SQL),
                    {"satellite_id": satellite["id"], "history_since": history_since},
                ).mappings()]
                telecommands = [dict(t) for t in conn.execute(
                    text(_TELECOMMANDS_SQL), {"satellite_id": satellite["id"]}
                ).mappings()]
        except Exception as error:
            logger.warning("Sem detalhe para %s: %s", code, error)
            return {"code": code, "database_available": False}

        by_pass: dict[Optional[int], list[dict[str, Any]]] = {}
        for telecommand in telecommands:
            by_pass.setdefault(telecommand["scheduled_pass_id"], []).append(
                _telecommand_to_dict(telecommand)
            )

        now = _now()
        return {
            "name": satellite["name"],
            "code": satellite["code"],
            "status": satellite["status"],
            "norad_id": satellite["norad_id"],
            "database_available": True,
            "station": _station_to_dict(self.station),
            "live": self._live_position(satellite),
            "tracking": {
                "azimuth_degrees": satellite["azimuth_deg"],
                "elevation_degrees": satellite["elevation_deg"],
                "range_km": satellite["range_km"],
                "is_visible": bool(satellite["is_visible"]),
                "status_message": satellite["status_message"],
                "checked_at": _isoformat(satellite["checked_at"]),
            },
            "scheduling": [
                _pass_detail(scheduled, by_pass.get(scheduled["id"], []))
                for scheduled in passes if scheduled["los_time"] >= now
            ],
            # Do mais recente para o mais antigo: no passado, o que acabou de
            # acontecer é o que o operador quer ver primeiro.
            "history": [
                _pass_detail(scheduled, by_pass.get(scheduled["id"], []))
                for scheduled in reversed(passes) if scheduled["los_time"] < now
            ],
            # Telecomandos que ainda não foram atribuídos a nenhuma passagem:
            # sem isso eles sumiriam da tela, que é o pior destino para um
            # comando que o operador criou e espera ver sair.
            "unscheduled_telecommands": by_pass.get(None, []),
        }

    # --- Revalidação dos dados orbitais -------------------------------------

    def refresh_orbital_data(self) -> dict[str, Any]:
        """Rebusca no CelesTrak os elementos de todos os satélites rastreáveis.

        É a única operação do painel que escreve alguma coisa — e escreve no
        cache de TLE (o volume compartilhado), nunca no banco. A regra continua
        de pé: quem escreve no Postgres é só o TC Scheduler. Como o cache é o
        mesmo volume que o Scheduler lê, o plano seguinte já sai com os
        elementos novos, sem que os dois processos precisem se conhecer.

        Um satélite que falha não interrompe os outros: o resultado traz uma
        linha por satélite, e a página mostra quem deu certo e quem não deu.
        """
        from sqlalchemy import text

        try:
            with self._engine.connect() as conn:
                satellites = [dict(r) for r in conn.execute(text(_TRACKABLE_SQL)).mappings()]
        except Exception as error:
            logger.warning("Sem lista de satélites para revalidar: %s", error)
            return {"database_available": False, "results": []}

        from spacelab_tracking import get_orbital_data

        results = []
        for satellite in satellites:
            # TLE manual é decisão explícita de operador e não vem do catálogo:
            # rebuscá-lo sobrescreveria justamente o que se quis fixar.
            if satellite["has_manual_tle"]:
                results.append(_refresh_result(satellite, "skipped", "TLE manual, não vem do catálogo"))
                continue
            try:
                data = get_orbital_data(satellite["norad_id"], force_update=True)
            except Exception as error:
                logger.warning("Falha ao revalidar %s: %s", satellite["code"], error)
                results.append(_refresh_result(satellite, "failed", str(error)))
                continue

            # O Satrec em memória foi construído dos elementos antigos: mantê-lo
            # faria o painel seguir mostrando a posição que se acabou de trocar.
            with self._cache_lock:
                self._satrec_cache.pop(satellite["code"], None)
            results.append(_refresh_result(satellite, "updated", None, source=data.source))

        updated = sum(1 for r in results if r["status"] == "updated")
        logger.info("Revalidação de TLE: %d de %d atualizados", updated, len(results))
        return {"database_available": True, "results": results, "updated": updated}

    def _live_position(self, satellite: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Vetor de estado e ponto subsatélite agora, ou None se não dá para propagar."""
        try:
            satrec = self._satrec_for(satellite)
        except Exception as error:
            logger.warning("Sem dados orbitais para %s: %s", satellite["code"], error)
            return None

        if satrec is None:
            return None

        try:
            from spacelab_tracking import get_tracking_info

            info = get_tracking_info(satrec, satellite["name"], station=self.station)
        except Exception as error:
            logger.warning("Falha ao propagar %s: %s", satellite["code"], error)
            return None

        return {
            "time_utc": _isoformat(info.time_utc),
            "tle_epoch": _isoformat(info.tle_epoch),
            "norad_id": info.norad_id,
            "position_teme_km": list(info.position_teme_km),
            "velocity_teme_km_s": list(info.velocity_teme_km_s),
            "geodetic": {
                "latitude_degrees": info.geodetic.latitude_deg,
                "longitude_degrees": info.geodetic.longitude_deg,
                "altitude_km": info.geodetic.altitude_km,
            },
            "topocentric": {
                "azimuth_degrees": info.topocentric.azimuth_deg,
                "elevation_degrees": info.topocentric.elevation_deg,
                "range_km": info.topocentric.range_km,
            },
            "is_visible": info.is_visible,
        }

    def _satrec_for(self, satellite: dict[str, Any]) -> Optional[Any]:
        """Satrec do satélite, reaproveitado entre aberturas do modal.

        Um TLE preenchido à mão tem precedência sobre o catálogo público: é uma
        decisão explícita de operador, e sobrepô-la anularia o motivo de tê-la
        tomado. Mesma regra do TC Scheduler.
        """
        if not (satellite["norad_id"] or (satellite["tle_line1"] and satellite["tle_line2"])):
            return None

        code = satellite["code"]
        with self._cache_lock:
            cached = self._satrec_cache.get(code)
            if cached is not None and time.monotonic() - cached[1] < SATREC_TTL_SECONDS:
                return cached[0]

        from spacelab_tracking import build_satellite, from_tle_lines, get_orbital_data

        if satellite["tle_line1"] and satellite["tle_line2"]:
            orbital_data = from_tle_lines(
                satellite["tle_line1"], satellite["tle_line2"], satellite["name"]
            )
        else:
            orbital_data = get_orbital_data(satellite["norad_id"])

        satrec = build_satellite(orbital_data)
        with self._cache_lock:
            self._satrec_cache[code] = (satrec, time.monotonic())
        return satrec

    def close(self) -> None:
        self._engine.dispose()


def _satellite_to_dict(
    satellite: dict[str, Any], next_pass: Optional[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "name": satellite["name"],
        "code": satellite["code"],
        "status": satellite["status"],
        "norad_id": satellite["norad_id"],
        "is_trackable": bool(satellite["is_trackable"]),
        "azimuth_degrees": satellite["azimuth_deg"],
        "elevation_degrees": satellite["elevation_deg"],
        "range_km": satellite["range_km"],
        "is_visible": bool(satellite["is_visible"]),
        "status_message": satellite["status_message"],
        "checked_at": _isoformat(satellite["checked_at"]),
        "next_pass": _pass_to_dict(next_pass),
    }


def _pass_to_dict(scheduled: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if scheduled is None:
        return None
    return {
        "aos_time": _isoformat(scheduled["aos_time"]),
        "los_time": _isoformat(scheduled["los_time"]),
        "max_elevation_degrees": scheduled["max_elevation_deg"],
        "status": scheduled["status"],
        "telecommand_count": scheduled["telecommand_count"],
    }


def _pass_detail(scheduled: dict[str, Any], telecommands: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": scheduled["id"],
        "aos_time": _isoformat(scheduled["aos_time"]),
        "los_time": _isoformat(scheduled["los_time"]),
        "culmination_time": _isoformat(scheduled["culmination_time"]),
        "max_elevation_degrees": scheduled["max_elevation_deg"],
        "status": scheduled["status"],
        "status_message": scheduled["status_message"],
        "pending": [t for t in telecommands if t["status"] in PENDING_STATUSES],
        "sent": [t for t in telecommands if t["status"] == "sent"],
        "telecommand_count": len(telecommands),
    }


def _telecommand_to_dict(telecommand: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": telecommand["id"],
        "command_type": telecommand["command_type"],
        "status": telecommand["status"],
        "priority": telecommand["priority"],
    }


def _refresh_result(
    satellite: dict[str, Any], status: str, message: Optional[str], source: Optional[str] = None
) -> dict[str, Any]:
    return {
        "name": satellite["name"],
        "code": satellite["code"],
        "norad_id": satellite["norad_id"],
        "status": status,
        "message": message,
        "source": source,
    }


def _station_to_dict(station: dict) -> dict[str, Any]:
    return {
        "name": station.get("name"),
        "latitude_degrees": station.get("latitude_deg"),
        "longitude_degrees": station.get("longitude_deg"),
        "altitude_m": station.get("altitude_m"),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None
