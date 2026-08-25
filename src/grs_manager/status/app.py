"""Painel de status do GRS Manager.

Checa a saúde de todo o pipeline até o rotor perguntando ao Station Manager:
`get_position()` só tem sucesso se a cadeia inteira (Station Manager -> Rotor
Manager -> rotor físico/simulado) estiver respondendo, e o valor devolvido é a
posição atual do rotor.

A página junta duas perguntas que o operador faz ao mesmo tempo: "para onde a
antena está apontada" (o rotor, daqui mesmo) e "para onde ela deveria estar
apontada" (satélites e plano de passagens, lidos do banco por `station_data`).
A segunda metade é opcional: sem PG_DATABASE_URL o painel mostra só o rotor.

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

from flask import Flask, Response, jsonify

from grs_manager.adapters.station_manager_zmq import StationManagerZmqClient
from grs_manager.domain.models import RotorPosition

POSITION_DECIMALS = 2
SSE_INTERVAL_SECONDS = 2

# Criar e editar telecomando é função do TC Generator: ele já tem os
# formulários, a validação e a sessão de operador. O painel manda o operador
# para lá em vez de duplicar isso — e, principalmente, em vez de dar ao GRS
# Manager uma escrita no banco que a arquitetura reserva ao TC Scheduler.
# O endereço é resolvido pelo navegador do operador, não pelo container, então
# o default é localhost e não o nome do serviço no compose.
TC_GENERATOR_URL = os.getenv("TC_GENERATOR_URL", "http://localhost:5000")


def create_app(
    station_manager_client: StationManagerZmqClient,
    station_data: Optional[Any] = None,
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
        """Satélites e próximas passagens. 200 mesmo sem banco: a página
        precisa distinguir "não configurado" de "falhou", e nenhum dos dois é
        erro do cliente."""
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
            return jsonify({"error": "painel sem acesso ao banco"}), 404
        return jsonify(station_data.refresh_orbital_data())

    @app.get("/api/satellite/<code>")
    def satellite(code: str):
        if station_data is None:
            return jsonify({"error": "painel sem acesso ao banco"}), 404
        detail = station_data.satellite_detail(code)
        if detail is None:
            return jsonify({"error": f"satélite {code} não encontrado"}), 404
        return jsonify(detail)

    @app.get("/")
    def status():
        return _render_page(**current_status())

    return app


def _position_to_dict(position: RotorPosition | None) -> dict[str, float] | None:
    if position is None:
        return None
    return {"azimuth_degrees": position.azimuth_degrees, "elevation_degrees": position.elevation_degrees}


def _render_page(
    rotor_connected: bool,
    rotor_position: dict[str, float] | None,
) -> str:
    return f"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GRS Manager — Status</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
  <main class="wrap">
    <div class="card">
      <h1>GRS Manager — Status da ponte de rotor</h1>
      {_status_row("rotor", "Rotor (via Station Manager)", rotor_connected, rotor_position, "sem leitura")}
      <footer><span id="live-dot" class="live-dot"></span><span id="live-label">ao vivo</span></footer>
    </div>

    <section class="satellites">
      <div class="section-head">
        <h2>Satélites</h2>
        <span id="sat-meta" class="muted"></span>
        <button id="refresh-tle" class="action" type="button">Atualizar TLE</button>
      </div>
      <div id="refresh-report" class="refresh-report" hidden></div>
      <div id="sat-grid" class="grid">
        <p class="muted">Carregando…</p>
      </div>
    </section>
  </main>

  <div id="modal" class="modal" hidden>
    <div class="modal-backdrop" data-close></div>
    <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <header class="modal-head">
        <h2 id="modal-title"></h2>
        <span id="modal-norad" class="muted mono"></span>
        <button class="close" data-close aria-label="Fechar">&times;</button>
      </header>
      <div id="modal-body" class="modal-body"></div>
    </div>
  </div>

<script>
const TC_GENERATOR_URL = {json.dumps(TC_GENERATOR_URL)};
{_PAGE_JS}
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


# CSS e JS ficam fora do f-string da página: dentro dela, cada `{` de uma regra
# CSS ou de um bloco JS precisaria ser escrito `{{`, o que tornaria os dois
# ilegíveis e quase impossíveis de editar sem introduzir erro.
_PAGE_CSS = """
  :root {
    color-scheme: light dark;
    --bg: #f4f5f7;
    --surface: #ffffff;
    --surface-2: #fafbfc;
    --text: #1c1e21;
    --muted: #7c7f83;
    --border: #e6e8ea;
    --accent: #2a7ab0;
    --ok: #2ecc71;
    --bad: #e74c3c;
    --warn: #e2892f;
    --shadow: 0 2px 12px rgba(0,0,0,0.08);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #101317;
      --surface: #171b21;
      --surface-2: #1d222a;
      --text: #e4e6eb;
      --muted: #8a8d91;
      --border: #2b313a;
      --accent: #4db3e8;
      --shadow: 0 2px 12px rgba(0,0,0,0.4);
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text);
    margin: 0; padding: 2rem 1.25rem 4rem;
  }
  .wrap { max-width: 1180px; margin: 0 auto; display: flex; flex-direction: column; gap: 2rem; }
  .mono { font-variant-numeric: tabular-nums; }
  .muted { color: var(--muted); }

  .card {
    background: var(--surface); border-radius: 12px; padding: 1.5rem 1.75rem;
    box-shadow: var(--shadow); border: 1px solid var(--border);
  }
  h1 { font-size: 1.05rem; margin: 0 0 1.25rem; color: var(--muted); font-weight: 600; }
  .row { display: flex; align-items: flex-start; gap: 0.75rem; padding: 0.7rem 0; }
  .row + .row { border-top: 1px solid var(--border); }
  .row-text { flex: 1; }
  .row-top { display: flex; align-items: center; }
  .dot { width: 11px; height: 11px; border-radius: 50%; flex-shrink: 0; margin-top: 0.3rem; transition: background-color 0.2s; }
  .dot-on { background: var(--ok); }
  .dot-off { background: var(--bad); }
  .label { flex: 1; font-size: 0.95rem; }
  .state { font-weight: 700; font-size: 0.8rem; letter-spacing: 0.03em; text-transform: uppercase; transition: color 0.2s; }
  .state.dot-on { color: var(--ok); background: none; }
  .state.dot-off { color: var(--bad); background: none; }
  .detail { font-size: 0.85rem; color: var(--muted); margin-top: 0.15rem; font-variant-numeric: tabular-nums; }
  footer { margin-top: 1.25rem; font-size: 0.75rem; color: var(--muted); display: flex; align-items: center; gap: 0.4rem; }
  .live-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); }
  .live-dot.stale { background: var(--muted); }

  .section-head { display: flex; align-items: baseline; gap: 0.75rem; margin-bottom: 0.9rem; }
  h2 { font-size: 1.05rem; margin: 0; font-weight: 600; }
  .section-head .muted { font-size: 0.8rem; flex: 1; }
  .action {
    background: none; border: 1px solid var(--border); border-radius: 8px;
    color: var(--accent); font: inherit; font-size: 0.82rem; padding: 0.35rem 0.8rem;
    cursor: pointer; transition: border-color 0.15s;
  }
  .action:hover:not(:disabled) { border-color: var(--accent); }
  .action:disabled { color: var(--muted); cursor: progress; }
  .refresh-report {
    background: var(--surface); border: 1px solid var(--border);
    border-left: 3px solid var(--accent); border-radius: 0 8px 8px 0;
    padding: 0.8rem 1rem; margin-bottom: 0.9rem; font-size: 0.85rem;
  }
  .refresh-report[hidden] { display: none; }
  .refresh-report ul { margin: 0.5rem 0 0; padding-left: 1.1rem; color: var(--muted); }
  .refresh-report li { margin-bottom: 0.15rem; }
  .refresh-report .failed { color: var(--bad); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(255px, 1fr)); gap: 1rem; }

  .sat {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 1.1rem 1.25rem; box-shadow: var(--shadow);
    cursor: pointer; text-align: left; width: 100%; font: inherit; color: inherit;
    transition: border-color 0.15s, transform 0.15s;
  }
  .sat:hover, .sat:focus-visible { border-color: var(--accent); transform: translateY(-2px); outline: none; }
  .sat-top { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.9rem; }
  .sat-name { font-weight: 600; flex: 1; }
  .sat-code { font-size: 0.72rem; color: var(--muted); letter-spacing: 0.04em; }
  .metric { display: flex; justify-content: space-between; font-size: 0.85rem; padding: 0.22rem 0; font-variant-numeric: tabular-nums; }
  .metric span:first-child { color: var(--muted); }
  .sat-foot { margin-top: 0.9rem; padding-top: 0.7rem; border-top: 1px solid var(--border); font-size: 0.82rem; color: var(--warn); }
  .sat-foot.none { color: var(--muted); }
  .badge { font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.05em; padding: 0.12rem 0.45rem; border-radius: 99px; border: 1px solid var(--border); color: var(--muted); }
  .badge.visible { color: var(--ok); border-color: var(--ok); }

  .modal { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center; padding: 1.5rem; z-index: 10; }
  .modal[hidden] { display: none; }
  .modal-backdrop { position: absolute; inset: 0; background: rgba(0,0,0,0.55); }
  .modal-box {
    position: relative; background: var(--surface); border: 1px solid var(--border);
    border-radius: 14px; box-shadow: 0 10px 40px rgba(0,0,0,0.35);
    width: min(1080px, 100%); max-height: 88vh; overflow-y: auto;
  }
  .modal-head { display: flex; align-items: center; gap: 1rem; padding: 1.25rem 1.5rem; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: var(--surface); border-radius: 14px 14px 0 0; }
  .modal-head h2 { color: var(--accent); flex-shrink: 0; }
  .modal-head .muted { flex: 1; font-size: 0.85rem; }
  .close { background: none; border: 1px solid var(--border); color: var(--muted); font-size: 1.3rem; line-height: 1; border-radius: 8px; width: 34px; height: 34px; cursor: pointer; }
  .close:hover { color: var(--text); border-color: var(--accent); }
  .modal-body { padding: 1.5rem; display: flex; flex-direction: column; gap: 1.5rem; }

  .panels { display: grid; grid-template-columns: repeat(auto-fit, minmax(215px, 1fr)); gap: 1rem; }
  .panel { background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px; padding: 1rem 1.1rem; }
  .panel h3 { margin: 0 0 0.7rem; font-size: 0.85rem; font-weight: 600; }

  .tabs { display: flex; gap: 1.25rem; border-bottom: 1px solid var(--border); }
  .tab { background: none; border: none; border-bottom: 2px solid transparent; color: var(--muted); font: inherit; font-size: 0.9rem; padding: 0.5rem 0; cursor: pointer; }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }

  .passes { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 1rem; }
  .pass { background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px; padding: 1rem 1.1rem; }
  .pass.selected { border-color: var(--accent); }
  .pass-when { color: var(--warn); font-weight: 600; font-size: 0.95rem; }
  .pass-at { font-size: 0.8rem; color: var(--muted); margin: 0.3rem 0 0.9rem; font-variant-numeric: tabular-nums; }
  .pass-metric { display: flex; justify-content: space-between; font-size: 0.82rem; padding: 0.18rem 0; }
  .pass-metric span:first-child { color: var(--muted); }
  .pass button.expand { margin-top: 0.8rem; width: 100%; background: none; border: 1px solid var(--border); border-radius: 8px; color: var(--accent); font: inherit; font-size: 0.82rem; padding: 0.4rem; cursor: pointer; }
  .pass button.expand:hover { border-color: var(--accent); }

  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th { text-align: left; font-weight: 600; color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; padding-bottom: 0.5rem; border-bottom: 1px solid var(--border); }
  td { padding: 0.5rem 0; border-bottom: 1px solid var(--border); font-variant-numeric: tabular-nums; }
  .tc-lists { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.25rem; }
  .link { color: var(--accent); font-size: 0.82rem; text-decoration: none; }
  .link:hover { text-decoration: underline; }
  .empty { color: var(--muted); font-size: 0.85rem; padding: 0.75rem 0; }
  .notice { background: var(--surface-2); border: 1px solid var(--border); border-left: 3px solid var(--warn); border-radius: 6px; padding: 0.7rem 0.9rem; font-size: 0.82rem; color: var(--muted); }
"""


_PAGE_JS = """
const REFRESH_MS = 15000;

function fmt(values) {
  return values ? `az ${values.azimuth_degrees.toFixed(2)}°  el ${values.elevation_degrees.toFixed(2)}°` : null;
}
function applyRow(id, active, values, placeholder) {
  const dot = document.getElementById(id + "-dot");
  const state = document.getElementById(id + "-state");
  const detail = document.getElementById(id + "-detail");
  dot.className = "dot " + (active ? "dot-on" : "dot-off");
  state.className = "state " + (active ? "dot-on" : "dot-off");
  state.textContent = active ? "ativo" : "inativo";
  detail.textContent = fmt(values) || placeholder;
}

const liveDot = document.getElementById("live-dot");
const liveLabel = document.getElementById("live-label");
const source = new EventSource("/events");
source.onmessage = (event) => {
  const data = JSON.parse(event.data);
  applyRow("rotor", data.rotor_connected, data.rotor_position, "sem leitura");
  liveDot.classList.remove("stale");
  liveLabel.textContent = "ao vivo";
};
source.onerror = () => {
  liveDot.classList.add("stale");
  liveLabel.textContent = "conexão perdida, tentando reconectar...";
};

// --- Formatação -----------------------------------------------------------

const num = (value, digits, unit) =>
  value === null || value === undefined ? "—" : value.toFixed(digits) + (unit || "");

function stamp(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// "in 5 min" / "há 5 min". Minutos, e não segundos, porque a previsão vem de
// um TLE com incerteza bem maior que isso -- exibir segundos sugeriria uma
// precisão que o dado não tem.
function relative(iso) {
  if (!iso) return null;
  const minutes = Math.round((new Date(iso) - Date.now()) / 60000);
  if (minutes >= 0) return `em ${minutes} min`;
  return `há ${Math.abs(minutes)} min`;
}

// O que o operador precisa saber sobre a próxima passagem, numa linha: se ela
// já começou, o que importa é quando termina; se não, quando começa.
function passCountdown(pass) {
  if (!pass) return null;
  const started = new Date(pass.aos_time) <= Date.now();
  return started ? `LOS ${relative(pass.los_time)}` : `AOS ${relative(pass.aos_time)}`;
}

// --- Grade ----------------------------------------------------------------

let satellites = [];

function satelliteCard(sat) {
  const card = document.createElement("button");
  card.className = "sat";
  card.type = "button";
  card.addEventListener("click", () => openModal(sat.code));

  const untrackable = !sat.is_trackable;
  const noFix = sat.elevation_degrees === null || sat.elevation_degrees === undefined;
  let detail;
  if (untrackable) {
    detail = `<p class="empty">Sem dados orbitais. Cadastre o NORAD ID para rastrear.</p>`;
  } else if (sat.status_message) {
    detail = `<p class="empty">${sat.status_message}</p>`;
  } else if (noFix) {
    detail = `<p class="empty">Aguardando o primeiro ciclo de rastreamento.</p>`;
  } else {
    detail = `
      <div class="metric"><span>Azimute</span><span>${num(sat.azimuth_degrees, 2, " °")}</span></div>
      <div class="metric"><span>Elevação</span><span>${num(sat.elevation_degrees, 2, " °")}</span></div>
      <div class="metric"><span>Distância</span><span>${num(sat.range_km, 2, " km")}</span></div>`;
  }

  const countdown = passCountdown(sat.next_pass);
  const foot = countdown
    ? `<div class="sat-foot" data-countdown="${sat.code}">${countdown}</div>`
    : `<div class="sat-foot none">sem passagem planejada</div>`;

  card.innerHTML = `
    <div class="sat-top">
      <span class="sat-name">${sat.name}</span>
      <span class="sat-code">${sat.code}</span>
      ${sat.is_visible ? '<span class="badge visible">visível</span>' : ""}
    </div>
    ${detail}
    ${foot}`;
  return card;
}

function renderGrid(payload) {
  const grid = document.getElementById("sat-grid");
  const meta = document.getElementById("sat-meta");
  satellites = payload.satellites || [];

  if (payload.configured === false) {
    grid.innerHTML = `<p class="notice">Painel sem acesso ao banco: defina PG_DATABASE_URL
      no serviço grs-manager para ver satélites e agendamentos.</p>`;
    meta.textContent = "";
    return;
  }
  if (!payload.database_available) {
    grid.innerHTML = `<p class="notice">Banco indisponível. O controle do rotor acima
      continua funcionando.</p>`;
    meta.textContent = "";
    return;
  }
  if (satellites.length === 0) {
    grid.innerHTML = `<p class="empty">Nenhum satélite cadastrado.</p>`;
    return;
  }

  grid.replaceChildren(...satellites.map(satelliteCard));
  const tracked = satellites.filter((s) => s.is_trackable).length;
  meta.textContent = `${tracked} de ${satellites.length} com dados orbitais`;
}

async function refreshGrid() {
  try {
    renderGrid(await (await fetch("/api/station")).json());
  } catch (error) {
    document.getElementById("sat-meta").textContent = "sem atualização";
  }
}

// Os contadores andam entre os polls, sem bater no servidor a cada segundo.
function tickCountdowns() {
  for (const sat of satellites) {
    const node = document.querySelector(`[data-countdown="${sat.code}"]`);
    if (node) node.textContent = passCountdown(sat.next_pass);
  }
}

// --- Modal ----------------------------------------------------------------

const modal = document.getElementById("modal");
let openCode = null;
let activeTab = "scheduling";
let expandedPass = null;

function closeModal() {
  modal.hidden = true;
  openCode = null;
}
modal.addEventListener("click", (event) => {
  if (event.target.hasAttribute("data-close")) closeModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !modal.hidden) closeModal();
});

async function openModal(code) {
  openCode = code;
  activeTab = "scheduling";
  expandedPass = null;
  modal.hidden = false;
  document.getElementById("modal-title").textContent = code;
  document.getElementById("modal-norad").textContent = "";
  document.getElementById("modal-body").innerHTML = `<p class="empty">Carregando…</p>`;

  try {
    const response = await fetch(`/api/satellite/${encodeURIComponent(code)}`);
    const detail = await response.json();
    if (!response.ok) throw new Error(detail.error || "falha ao carregar");
    if (openCode === code) renderModal(detail);
  } catch (error) {
    document.getElementById("modal-body").innerHTML =
      `<p class="notice">Não foi possível carregar: ${error.message}</p>`;
  }
}

function panel(title, rows) {
  const body = rows
    .map(([label, value]) => `<div class="metric"><span>${label}</span><span>${value}</span></div>`)
    .join("");
  return `<div class="panel"><h3>${title}</h3>${body}</div>`;
}

function statePanels(detail) {
  const live = detail.live;
  const station = detail.station || {};
  const tracking = detail.tracking || {};

  if (!live) {
    return `<p class="notice">Sem dados orbitais para propagar a posição deste satélite.
      Cadastre o NORAD ID em <a class="link" href="${TC_GENERATOR_URL}/satellites"
      target="_blank" rel="noopener">TC Generator › Satellites</a>.</p>`;
  }

  const [x, y, z] = live.position_teme_km;
  const [vx, vy, vz] = live.velocity_teme_km_s;
  return `<div class="panels">
    ${panel("ECI/TEME", [
      ["X (km)", num(x, 3)], ["Y (km)", num(y, 3)], ["Z (km)", num(z, 3)],
      ["Vx (km/s)", num(vx, 4)], ["Vy (km/s)", num(vy, 4)], ["Vz (km/s)", num(vz, 4)],
    ])}
    ${panel("Geodetic", [
      ["Latitude", num(live.geodetic.latitude_degrees, 4, " °")],
      ["Longitude", num(live.geodetic.longitude_degrees, 4, " °")],
      ["Altitude", num(live.geodetic.altitude_km, 2, " km")],
    ])}
    ${panel("Ground Station", [
      ["Nome", station.name || "—"],
      ["Latitude", num(station.latitude_degrees, 4, " °")],
      ["Longitude", num(station.longitude_degrees, 4, " °")],
      ["Altitude", num(station.altitude_m, 1, " m")],
    ])}
    ${panel("Tracking", [
      ["Azimute", num(live.topocentric.azimuth_degrees, 2, " °")],
      ["Elevação", num(live.topocentric.elevation_degrees, 2, " °")],
      ["Distância", num(live.topocentric.range_km, 2, " km")],
      ["TLE epoch", stamp(live.tle_epoch)],
      ["Gravado em", stamp(tracking.checked_at)],
    ])}
  </div>`;
}

function passCard(pass, isHistory) {
  const when = isHistory
    ? `LOS ${relative(pass.los_time)}`
    : passCountdown({ aos_time: pass.aos_time, los_time: pass.los_time });
  const expanded = expandedPass === pass.id;
  return `<div class="pass ${expanded ? "selected" : ""}">
    <div class="pass-when">${when}</div>
    <div class="pass-at">${stamp(pass.aos_time)}</div>
    <div class="pass-metric"><span>Elevação máx.</span><span>${num(pass.max_elevation_degrees, 1, " °")}</span></div>
    <div class="pass-metric"><span>Situação</span><span>${pass.status}</span></div>
    <div class="pass-metric"><span>A enviar</span><span>${pass.pending.length}</span></div>
    <div class="pass-metric"><span>Enviados</span><span>${pass.sent.length}</span></div>
    <button class="expand" data-pass="${pass.id}">
      ${expanded ? "ocultar comandos" : "ver comandos"}
    </button>
  </div>`;
}

function telecommandTable(title, telecommands) {
  if (telecommands.length === 0) {
    return `<div><h3>${title}</h3><p class="empty">nenhum</p></div>`;
  }
  const rows = telecommands
    .map((tc) => `<tr><td class="mono">#${tc.id}</td><td>${tc.command_type}</td>
      <td class="muted">prio ${tc.priority}</td></tr>`)
    .join("");
  return `<div><h3>${title}</h3><table>
    <thead><tr><th>ID</th><th>Tipo</th><th>Prioridade</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function expandedPassBlock(passes) {
  const pass = passes.find((p) => p.id === expandedPass);
  if (!pass) return "";
  return `<div class="panel">
    <h3>Passagem de ${stamp(pass.aos_time)} — ${stamp(pass.los_time)}</h3>
    <div class="tc-lists">
      ${telecommandTable("A enviar", pass.pending)}
      ${telecommandTable("Enviados", pass.sent)}
    </div>
  </div>`;
}

function renderModal(detail) {
  // O banco pode cair entre a grade e o clique: aí o detalhe volta sem
  // nenhuma das listas, e insistir em renderizá-las quebraria o modal.
  if (detail.database_available === false) {
    document.getElementById("modal-body").innerHTML =
      `<p class="notice">Banco indisponível. Tente de novo em instantes.</p>`;
    return;
  }

  document.getElementById("modal-title").textContent = detail.name;
  document.getElementById("modal-norad").textContent =
    detail.norad_id ? `NORAD ${detail.norad_id}` : "sem NORAD ID";

  const passes = activeTab === "scheduling" ? detail.scheduling : detail.history;
  const list = passes.length
    ? `<div class="passes">${passes.map((p) => passCard(p, activeTab === "history")).join("")}</div>`
    : `<p class="empty">${activeTab === "scheduling"
        ? "Nenhuma passagem planejada."
        : "Nenhuma passagem nas últimas 24 h."}</p>`;

  const unscheduled = detail.unscheduled_telecommands || [];
  const backlog = unscheduled.length
    ? `<div class="panel">
         <h3>Sem passagem atribuída</h3>
         <p class="empty">Entram no plano no próximo ciclo do Scheduler.</p>
         ${telecommandTable("Na fila", unscheduled)}
       </div>`
    : "";

  document.getElementById("modal-body").innerHTML = `
    ${statePanels(detail)}
    <div class="tabs">
      <button class="tab ${activeTab === "scheduling" ? "active" : ""}" data-tab="scheduling">Agendamento</button>
      <button class="tab ${activeTab === "history" ? "active" : ""}" data-tab="history">Histórico (24 h)</button>
    </div>
    ${list}
    ${expandedPassBlock(passes)}
    ${backlog}
    <a class="link" href="${TC_GENERATOR_URL}" target="_blank" rel="noopener">
      Criar ou editar telecomandos no TC Generator ↗</a>`;

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      activeTab = tab.dataset.tab;
      expandedPass = null;
      renderModal(detail);
    });
  }
  for (const button of document.querySelectorAll("[data-pass]")) {
    button.addEventListener("click", () => {
      const id = Number(button.dataset.pass);
      expandedPass = expandedPass === id ? null : id;
      renderModal(detail);
    });
  }
}

// --- Revalidação de TLE ---------------------------------------------------

const refreshButton = document.getElementById("refresh-tle");
const refreshReport = document.getElementById("refresh-report");

function reportLine(result) {
  if (result.status === "updated") return `${result.name}: atualizado (${result.source})`;
  if (result.status === "skipped") return `${result.name}: ${result.message}`;
  return `<span class="failed">${result.name}: ${result.message}</span>`;
}

refreshButton.addEventListener("click", async () => {
  refreshButton.disabled = true;
  refreshButton.textContent = "Buscando no CelesTrak…";
  refreshReport.hidden = false;
  refreshReport.innerHTML = "<strong>Revalidando elementos orbitais…</strong>";

  try {
    const response = await fetch("/api/tle/refresh", { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "falha na revalidação");

    const results = data.results || [];
    if (results.length === 0) {
      refreshReport.innerHTML = "<strong>Nenhum satélite com dados orbitais para atualizar.</strong>";
    } else {
      refreshReport.innerHTML =
        `<strong>${data.updated} de ${results.length} atualizados no CelesTrak.</strong>` +
        `<ul>${results.map((r) => `<li>${reportLine(r)}</li>`).join("")}</ul>`;
    }
    // O Scheduler lê o mesmo cache: as posições da grade acompanham no ciclo
    // dele, mas o modal já abre com os elementos novos.
    refreshGrid();
  } catch (error) {
    refreshReport.innerHTML = `<span class="failed">Não foi possível atualizar: ${error.message}</span>`;
  } finally {
    refreshButton.disabled = false;
    refreshButton.textContent = "Atualizar TLE";
  }
});

refreshGrid();
setInterval(refreshGrid, REFRESH_MS);
setInterval(tickCountdowns, 1000);
"""
