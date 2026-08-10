"""Painel de status do GRS Manager.

O GRS Manager é o componente "GUI Software" no diagrama de arquitetura — é
ele quem sabe diretamente se o gpredict está conectado (dono do servidor
rotctld) e qual foi o último alvo que ele pediu (`P <az> <el>`), e pode
checar a saúde de todo o pipeline até o rotor perguntando ao Station
Manager: `get_position()` só tem sucesso se a cadeia inteira (Station
Manager -> Rotor Manager -> rotor físico/simulado) estiver respondendo, e o
valor devolvido é a posição atual do rotor.

Usa um StationManagerZmqClient dedicado (separado do que atende o gpredict de
verdade) — assim, uma checagem de saúde que dê timeout não deixa o socket REQ
usado pelo tráfego real do gpredict num estado inconsistente (ver limitação
documentada em `grs_manager.adapters.station_manager_zmq`).
"""

from __future__ import annotations

from flask import Flask

from grs_manager.adapters.station_manager_zmq import StationManagerZmqClient
from grs_manager.domain.models import RotorPosition
from grs_manager.rotctld.server import RotctldServer

POSITION_DECIMALS = 2


def create_app(rotctld_server: RotctldServer, station_manager_client: StationManagerZmqClient) -> Flask:
    app = Flask(__name__)

    def rotor_position() -> RotorPosition | None:
        try:
            return station_manager_client.get_position()
        except Exception:
            return None

    @app.get("/health")
    def health():
        target = rotctld_server.last_target_from_gpredict
        position = rotor_position()
        return {
            "gpredict_connected": rotctld_server.is_gpredict_connected,
            "gpredict_last_target": _target_to_dict(target),
            "rotor_connected": position is not None,
            "rotor_position": _position_to_dict(position),
        }

    @app.get("/")
    def status():
        target = rotctld_server.last_target_from_gpredict
        position = rotor_position()
        return _render_page(
            gpredict_connected=rotctld_server.is_gpredict_connected,
            gpredict_target=target,
            rotor_connected=position is not None,
            rotor_position=position,
        )

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


def _format_az_el(azimuth: float, elevation: float) -> str:
    return f"az {azimuth:.{POSITION_DECIMALS}f}°  el {elevation:.{POSITION_DECIMALS}f}°"


def _status_row(label: str, active: bool, detail: str | None) -> str:
    state = "ativo" if active else "inativo"
    dot_class = "dot-on" if active else "dot-off"
    detail_html = f'<div class="detail">{detail}</div>' if detail else ""
    return f"""
      <div class="row">
        <span class="dot {dot_class}"></span>
        <div class="row-text">
          <div class="row-top">
            <span class="label">{label}</span>
            <span class="state {dot_class}">{state}</span>
          </div>
          {detail_html}
        </div>
      </div>"""


def _render_page(
    gpredict_connected: bool,
    gpredict_target: tuple[float, float] | None,
    rotor_connected: bool,
    rotor_position: RotorPosition | None,
) -> str:
    gpredict_detail = _format_az_el(*gpredict_target) if gpredict_target else "nenhum comando recebido ainda"
    rotor_detail = (
        _format_az_el(rotor_position.azimuth_degrees, rotor_position.elevation_degrees)
        if rotor_position else "sem leitura"
    )
    return f"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
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
  .dot {{ width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0; margin-top: 0.3rem; }}
  .dot-on {{ background: #2ecc71; }}
  .dot-off {{ background: #e74c3c; }}
  .label {{ flex: 1; font-size: 0.95rem; }}
  .state {{ font-weight: 700; font-size: 0.8rem; letter-spacing: 0.03em; text-transform: uppercase; }}
  .state.dot-on {{ color: #2ecc71; background: none; }}
  .state.dot-off {{ color: #e74c3c; background: none; }}
  .detail {{ font-size: 0.85rem; color: #888; margin-top: 0.15rem; font-variant-numeric: tabular-nums; }}
  footer {{ margin-top: 1.5rem; font-size: 0.75rem; color: #999; }}
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
    {_status_row("gpredict (Satellite Tracker)", gpredict_connected, gpredict_detail)}
    {_status_row("Rotor (via Station Manager)", rotor_connected, rotor_detail)}
    <footer>Atualiza a cada 5s</footer>
  </div>
</body>
</html>"""
