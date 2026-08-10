"""Painel de status do GRS Manager.

O GRS Manager é o componente "GUI Software" no diagrama de arquitetura — é
ele quem sabe diretamente se o gpredict está conectado (dono do servidor
rotctld) e qual foi o último alvo que ele pediu (`P <az> <el>`), e pode
checar a saúde de todo o pipeline até o rotor perguntando ao Station
Manager: `get_position()` só tem sucesso se a cadeia inteira (Station
Manager -> Rotor Manager -> rotor físico/simulado) estiver respondendo, e o
valor devolvido é a posição atual do rotor.

A página `/` recebe atualizações ao vivo via Server-Sent Events (`/events`)
— uma única direção (servidor -> navegador), sem precisar de nenhuma
dependência nova (Flask/Werkzeug já suportam respostas em stream) nem da
complexidade de WebSocket bidirecional, que não é necessária aqui.

Usa um StationManagerZmqClient dedicado (separado do que atende o gpredict de
verdade) — assim, uma checagem de saúde que dê timeout não deixa o socket REQ
usado pelo tráfego real do gpredict num estado inconsistente (ver limitação
documentada em `grs_manager.adapters.station_manager_zmq`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from flask import Flask, Response

from grs_manager.adapters.station_manager_zmq import StationManagerZmqClient
from grs_manager.domain.models import RotorPosition
from grs_manager.rotctld.server import RotctldServer

POSITION_DECIMALS = 2
SSE_INTERVAL_SECONDS = 2


def create_app(rotctld_server: RotctldServer, station_manager_client: StationManagerZmqClient) -> Flask:
    app = Flask(__name__)

    def rotor_position() -> RotorPosition | None:
        try:
            return station_manager_client.get_position()
        except Exception:
            return None

    def current_status() -> dict[str, object]:
        target = rotctld_server.last_target_from_gpredict
        position = rotor_position()
        return {
            "gpredict_connected": rotctld_server.is_gpredict_connected,
            "gpredict_last_target": _target_to_dict(target),
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

    @app.get("/")
    def status():
        return _render_page(**current_status())

    return app


def _target_to_dict(target: tuple[float, float] | None) -> dict[str, float] | None:
    if target is None:
        return None
    azimuth, elevation = target
    return {"azimuth_degrees": azimuth, "elevation_degrees": elevation}


def _position_to_dict(position: RotorPosition | None) -> dict[str, float] | None:
    if position is None:
        return None
    return {"azimuth_degrees": position.azimuth_degrees, "elevation_degrees": position.elevation_degrees}


def _render_page(
    gpredict_connected: bool,
    gpredict_last_target: dict[str, float] | None,
    rotor_connected: bool,
    rotor_position: dict[str, float] | None,
) -> str:
    return f"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<title>GRS Manager — Status</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #f4f5f7; color: #1c1e21;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; margin: 0;
  }}
  .card {{
    background: #ffffff; border-radius: 12px; padding: 2rem 2.5rem;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08); min-width: 360px;
  }}
  h1 {{ font-size: 1.05rem; margin: 0 0 1.25rem; color: #555; font-weight: 600; }}
  .row {{ display: flex; align-items: flex-start; gap: 0.75rem; padding: 0.7rem 0; }}
  .row + .row {{ border-top: 1px solid #eee; }}
  .row-text {{ flex: 1; }}
  .row-top {{ display: flex; align-items: center; }}
  .dot {{ width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0; margin-top: 0.3rem; transition: background-color 0.2s; }}
  .dot-on {{ background: #2ecc71; }}
  .dot-off {{ background: #e74c3c; }}
  .label {{ flex: 1; font-size: 0.95rem; }}
  .state {{ font-weight: 700; font-size: 0.8rem; letter-spacing: 0.03em; text-transform: uppercase; transition: color 0.2s; }}
  .state.dot-on {{ color: #2ecc71; background: none; }}
  .state.dot-off {{ color: #e74c3c; background: none; }}
  .detail {{ font-size: 0.85rem; color: #888; margin-top: 0.15rem; font-variant-numeric: tabular-nums; }}
  footer {{ margin-top: 1.5rem; font-size: 0.75rem; color: #999; display: flex; align-items: center; gap: 0.4rem; }}
  .live-dot {{ width: 6px; height: 6px; border-radius: 50%; background: #2ecc71; }}
  .live-dot.stale {{ background: #999; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #18191a; color: #e4e6eb; }}
    .card {{ background: #242526; box-shadow: 0 2px 12px rgba(0,0,0,0.4); }}
    h1 {{ color: #b0b3b8; }}
    .row + .row {{ border-top-color: #3a3b3c; }}
    .detail {{ color: #8a8d91; }}
  }}
</style>
</head>
<body>
  <div class="card">
    <h1>GRS Manager — Status da ponte de rotor</h1>
    {_status_row("gpredict", "gpredict (Satellite Tracker)", gpredict_connected, gpredict_last_target, "nenhum comando recebido ainda")}
    {_status_row("rotor", "Rotor (via Station Manager)", rotor_connected, rotor_position, "sem leitura")}
    <footer><span id="live-dot" class="live-dot"></span><span id="live-label">ao vivo</span></footer>
  </div>
<script>
  function fmt(values) {{
    return values ? `az ${{values.azimuth_degrees.toFixed(2)}}°  el ${{values.elevation_degrees.toFixed(2)}}°` : null;
  }}
  function applyRow(id, active, values, placeholder) {{
    const dot = document.getElementById(id + "-dot");
    const state = document.getElementById(id + "-state");
    const detail = document.getElementById(id + "-detail");
    dot.className = "dot " + (active ? "dot-on" : "dot-off");
    state.className = "state " + (active ? "dot-on" : "dot-off");
    state.textContent = active ? "ativo" : "inativo";
    detail.textContent = fmt(values) || placeholder;
  }}
  const liveDot = document.getElementById("live-dot");
  const liveLabel = document.getElementById("live-label");
  const source = new EventSource("/events");
  source.onmessage = (event) => {{
    const data = JSON.parse(event.data);
    applyRow("gpredict", data.gpredict_connected, data.gpredict_last_target, "nenhum comando recebido ainda");
    applyRow("rotor", data.rotor_connected, data.rotor_position, "sem leitura");
    liveDot.classList.remove("stale");
    liveLabel.textContent = "ao vivo";
  }};
  source.onerror = () => {{
    liveDot.classList.add("stale");
    liveLabel.textContent = "conexão perdida, tentando reconectar...";
  }};
</script>
</body>
</html>"""


def _status_row(row_id: str, label: str, active: bool, values: dict[str, float] | None, placeholder: str) -> str:
    state = "ativo" if active else "inativo"
    dot_class = "dot-on" if active else "dot-off"
    detail = (
        f"az {values['azimuth_degrees']:.{POSITION_DECIMALS}f}°  el {values['elevation_degrees']:.{POSITION_DECIMALS}f}°"
        if values else placeholder
    )
    return f"""
      <div class="row">
        <span id="{row_id}-dot" class="dot {dot_class}"></span>
        <div class="row-text">
          <div class="row-top">
            <span class="label">{label}</span>
            <span id="{row_id}-state" class="state {dot_class}">{state}</span>
          </div>
          <div id="{row_id}-detail" class="detail">{detail}</div>
        </div>
      </div>"""
