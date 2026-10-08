"""Painel de status do GRS Manager.

Checa a saúde de todo o pipeline até o rotor perguntando ao Station Manager:
`get_position()` só tem sucesso se a cadeia inteira (Station Manager -> Rotor
Manager -> rotor físico/simulado) estiver respondendo, e o valor devolvido é a
posição atual do rotor.

A página junta duas perguntas que o operador faz ao mesmo tempo: "para onde a
antena está apontada" (o rotor, daqui mesmo) e "para onde ela deveria estar
apontada" (satélites e plano de passagens, pedidos ao TC Scheduler por
`scheduler_client`). A segunda metade é opcional: sem TC_SCHEDULER_API_URL, ou
com o Scheduler parado, o painel mostra só o rotor.

A aba Previsão é a única parte que escreve: pular, forçar ou desfazer uma
passagem, e ligar a recepção ou cadastrar os downlinks de um satélite (cada
portadora é ouvida pelo rádio da sua faixa). Mesmo assim o painel não toca no banco — repassa ao Scheduler, que
guarda a decisão e replaneja em segundos.

Não reporta o estado do servidor rotctld. Ele continua de pé na porta 4533 para
quem quiser assumir a antena por um cliente hamlib, mas o rastreamento da
estação é próprio (TC Scheduler decide, Station Manager aponta) — e um painel
que destacasse a conexão externa sugeriria que ela ainda faz parte do fluxo
normal, o que deixou de ser verdade.

Os dois lados atualizam em ritmos diferentes de propósito. O rotor vai por
Server-Sent Events (`/events`) a cada 2s, porque muda continuamente durante
uma passagem. Os satélites vão por polling de `/api/station`, porque quem os
escreve é o TC Scheduler a cada 30s — mandá-los no mesmo stream de 2s seria
reenviar quinze vezes o mesmo dado.

Usa um StationManagerZmqClient dedicado (separado do que atende o tráfego real
de rotor) — assim, uma checagem de saúde que dê timeout não deixa o socket REQ
de produção num estado inconsistente (ver limitação documentada em
`grs_manager.adapters.station_manager_zmq`).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from typing import Any, Optional

from flask import Flask, Response, jsonify, render_template, request

from grs_manager.adapters.station_manager_zmq import StationManagerZmqClient
from grs_manager.domain.models import RotorPosition
from grs_manager.status.scheduler_client import (
    SatelliteNotFound,
    SchedulerRejected,
    SchedulerUnavailable,
)

SSE_INTERVAL_SECONDS = 2

# Criar e editar telecomando é função do TC Generator: ele já tem os
# formulários, a validação e a sessão de operador. O painel manda o operador
# para lá em vez de duplicar isso — e, principalmente, em vez de dar ao GRS
# Manager uma escrita no banco que a arquitetura reserva ao TC Scheduler —
# banco que, aliás, este serviço não alcança mais nem para ler.
# O endereço é resolvido pelo navegador do operador, não pelo container, então
# o default é localhost e não o nome do serviço no compose.
TC_GENERATOR_URL = os.getenv("TC_GENERATOR_URL", "http://localhost:5000")


def create_app(
    station_manager_client: StationManagerZmqClient,
    station_data: Optional[Any] = None,
    telemetry: Optional[Any] = None,
) -> Flask:
    app = Flask(__name__)

    def rotor_position() -> RotorPosition | None:
        try:
            return station_manager_client.get_position()
        except Exception:
            return None

    def current_status() -> dict[str, object]:
        position = rotor_position()
        return {
            "rotor_connected": position is not None,
            "rotor_position": _position_to_dict(position),
        }

    @app.get("/health")
    def health():
        return current_status()

    @app.get("/events")
    def events():
        def stream() -> Iterator[str]:
            while True:
                yield f"data: {json.dumps(current_status())}\n\n"
                time.sleep(SSE_INTERVAL_SECONDS)

        return Response(stream(), mimetype="text/event-stream")

    @app.get("/api/station")
    def station():
        """Satélites e próximas passagens. 200 mesmo sem o Scheduler: a
        página precisa distinguir "não configurado" de "falhou", e nenhum dos
        dois é erro do cliente."""
        if station_data is None:
            return jsonify({"satellites": [], "database_available": False, "configured": False})
        return jsonify({**station_data.snapshot(), "configured": True})

    @app.post("/api/tle/refresh")
    def refresh_tle():
        """Revalida os elementos orbitais no CelesTrak, sem esperar o cache expirar.

        POST porque muda estado (o cache de TLE compartilhado) — um GET aqui
        seria revalidado por qualquer prefetch de navegador.
        """
        if station_data is None:
            return jsonify({"error": "painel sem acesso ao TC Scheduler"}), 404
        return jsonify(station_data.refresh_orbital_data())

    @app.get("/api/satellite/<code>")
    def satellite(code: str):
        if station_data is None:
            return jsonify({"error": "painel sem acesso ao TC Scheduler"}), 404
        detail = station_data.satellite_detail(code)
        if detail is None:
            return jsonify({"error": f"satélite {code} não encontrado"}), 404
        return jsonify(detail)

    @app.get("/api/passes")
    def passes():
        """Passagens previstas no horizonte, escolhidas ou não. 200 mesmo sem o
        Scheduler, pelo mesmo motivo de `/api/station`."""
        if station_data is None:
            return jsonify({"passes": [], "reception": [], "computed_at": None,
                            "database_available": False, "configured": False})
        return jsonify({**station_data.passes(), "configured": True})

    def operator_action(action):
        """Repassa uma ação do operador e traduz o que o Scheduler respondeu.

        Os três desfechos de erro chegam à tela com códigos diferentes: 400 é o
        operador que precisa corrigir o pedido (a mensagem do Scheduler vai
        como está), 404 é satélite que não existe, 503 é a estação sem acesso.
        """
        if station_data is None:
            return jsonify({"error": "painel sem acesso ao TC Scheduler"}), 503
        try:
            return jsonify(action())
        except SchedulerRejected as error:
            return jsonify({"error": str(error)}), 400
        except SatelliteNotFound as error:
            return jsonify({"error": str(error)}), 404
        except SchedulerUnavailable:
            return jsonify({"error": "TC Scheduler indisponível; a ação não foi aplicada"}), 503

    @app.put("/api/satellites/<code>/reception")
    def reception(code: str):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "esperava um objeto JSON"}), 400
        return operator_action(lambda: station_data.set_reception(code, body))

    @app.put("/api/satellites/<code>/downlinks")
    def downlinks(code: str):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("downlinks"), list):
            return jsonify({"error": 'envie {"downlinks": [...]}'}), 400
        return operator_action(lambda: station_data.set_downlinks(code, body["downlinks"]))

    @app.put("/api/passes/decision")
    def decision():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not body.get("satellite_code") or not body.get("aos"):
            return jsonify({"error": "informe satellite_code e aos"}), 400
        return operator_action(lambda: station_data.set_decision(
            body["satellite_code"], body["aos"], body.get("decision")))

    @app.get("/api/telemetry/latest")
    def telemetry_latest():
        """O General Telemetry mais recente do FS-2, do grs-telemetry-decoder.
        Mesmo contrato de /api/station: `configured` diz se há decodificador,
        `available` se ele respondeu."""
        if telemetry is None:
            return jsonify({"configured": False, "available": False, "record": None})
        return jsonify({**telemetry.latest("fs2", "general_telemetry"), "configured": True})

    @app.get("/")
    def status():
        state = current_status()
        return render_template(
            "index.html",
            rotor_connected=state["rotor_connected"],
            rotor_position=state["rotor_position"],
            tc_generator_url=TC_GENERATOR_URL,
        )

    return app


def _position_to_dict(position: RotorPosition | None) -> dict[str, float] | None:
    if position is None:
        return None
    return {"azimuth_degrees": position.azimuth_degrees, "elevation_degrees": position.elevation_degrees}
