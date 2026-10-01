"""!panel: página web con estadísticas del servidor (música y League), con gráficos.

Genera un único archivo HTML (sin dependencias: los gráficos son SVG dibujados por un poco de
JavaScript incluido en la misma página), lo guarda en la carpeta panel/ y lo manda al canal como
archivo adjunto, para abrirlo con el navegador.

Uso:  !panel            todo el historial
      !panel 2026-09    solo ese mes (también "pasado", "septiembre"...)

Datos: data/escuchas.jsonl (pedidos y escuchas, el mismo que usa el Wrapped) y
data/partidas_lol.jsonl (partidas de las cuentas vinculadas).
"""

import html
import json
import logging
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

import riot_cuentas
import wrapped
from jsonio import read_jsonl
from letras import artist_of, song_name

log = logging.getLogger("panel")

PANEL_DIR = Path(__file__).resolve().parent / "panel"
DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


def _month_label(month: str) -> str:
    year, mon = month.split("-")
    return f"{wrapped.MESES[int(mon) - 1][:3].title()} {year[2:]}"


def build_data(guild: discord.Guild, month: Optional[str] = None) -> dict:
    """Junta todos los números del panel. month=None: todo el historial."""
    def in_scope(e: dict) -> bool:
        return e.get("g") in (guild.id, 0) and (month is None or e.get("t", "").startswith(month))

    events = [e for e in read_jsonl(wrapped.EVENTS_FILE) if in_scope(e)]
    requests = [e for e in events if e.get("tipo") == "pedido"]
    listens = [e for e in events if e.get("tipo") == "escucha"]
    seen, games = set(), []
    for g in read_jsonl(wrapped.LOL_FILE):
        if in_scope(g) and (g.get("u"), g.get("match")) not in seen:
            seen.add((g.get("u"), g.get("match")))
            games.append(g)

    names: dict[int, str] = {}
    for e in requests:
        names[e.get("u")] = e.get("un") or names.get(e.get("u"), "?")
    for uid, account in riot_cuentas.load().items():
        names.setdefault(int(uid), account.get("discord") or riot_cuentas.riot_id(account))

    def name(uid: int) -> str:
        member = guild.get_member(uid) if uid else None
        return member.display_name if member else names.get(uid, f"usuario {uid}")

    # Por mes (últimos 12 meses con datos)
    per_month = Counter(e["t"][:7] for e in listens or requests)
    hours_month = defaultdict(float)
    for e in listens:
        hours_month[e["t"][:7]] += e.get("seg", 0) / 3600
    months = sorted(per_month)[-12:]
    # Actividad por hora y por día (pedidos; los importados del historial viejo no tienen hora real)
    timed = [e for e in requests if not e.get("importado")]
    by_hour = Counter(datetime.fromisoformat(e["t"]).hour for e in timed)
    by_day = Counter(datetime.fromisoformat(e["t"]).weekday() for e in timed)
    source = listens or requests
    artists = Counter(artist_of(e["titulo"], e.get("canal_yt", "")) for e in source)
    artists.pop("", None)
    songs = Counter(song_name(e["titulo"]) for e in source)
    djs = Counter(e["u"] for e in requests)
    listeners = defaultdict(float)
    for e in listens:
        for uid in e.get("oyentes", []):
            listeners[uid] += e.get("seg", 0) / 3600

    # League
    by_player: dict[int, list[dict]] = defaultdict(list)
    for g in games:
        by_player[g["u"]].append(g)
    players = sorted(by_player.items(), key=lambda x: -len(x[1]))[:10]

    def kda(gs: list[dict]) -> float:
        return round((sum(g["k"] for g in gs) + sum(g["a"] for g in gs)) / max(1, sum(g["d"] for g in gs)), 2)

    return {
        "titulo": guild.name,
        "periodo": wrapped.month_label(month) if month else "Todo el historial",
        "generado": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "kpis": [
            {"label": "Canciones que sonaron", "value": len(listens)},
            {"label": "Horas de música", "value": round(sum(e.get("seg", 0) for e in listens) / 3600, 1)},
            {"label": "Canciones pedidas", "value": len(requests)},
            {"label": "Partidas de League", "value": len(games)},
        ],
        "meses": [{"label": _month_label(m), "value": per_month[m]} for m in months],
        "horas_mes": [{"label": _month_label(m), "value": round(hours_month[m], 1)} for m in months],
        "por_hora": [{"label": f"{h:02d}", "value": by_hour.get(h, 0)} for h in range(24)],
        "por_dia": [{"label": DIAS[d], "value": by_day.get(d, 0)} for d in range(7)],
        "artistas": [{"label": a, "value": n} for a, n in artists.most_common(10)],
        "canciones": [{"label": s, "value": n} for s, n in songs.most_common(10)],
        "djs": [{"label": name(u), "value": n} for u, n in djs.most_common(10)],
        "oyentes": [{"label": name(u), "value": round(h, 1)} for u, h in sorted(listeners.items(), key=lambda x: -x[1])[:10]],
        "lol_resultados": [{"label": name(u), "values": [sum(1 for g in gs if g.get("win")), sum(1 for g in gs if not g.get("win"))]}
                           for u, gs in players],
        "lol_kda": [{"label": name(u), "value": kda(gs)} for u, gs in players if len(gs) >= 3],
        "lol_campeones": [{"label": c, "value": n} for c, n in Counter(g["champ"] for g in games).most_common(10)],
        "lol_modos": [{"label": c or "?", "value": n} for c, n in Counter(g.get("cola") for g in games).most_common(8)],
    }


TEMPLATE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estadísticas · __TITLE__</title>
<style>
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--hover:rgba(11,11,11,.04)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--hover:rgba(255,255,255,.05)}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--grid:#2c2c2a;
--axis:#383835;--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--hover:rgba(255,255,255,.05)}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 16px 48px}
header{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:20px}
h1{font-size:26px;margin:0}h2{font-size:16px;margin:0 0 2px}.sub{color:var(--ink2);font-size:13px;margin:0 0 12px}
.meta{color:var(--muted);font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:12px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px;min-width:0}
.kpi .label{color:var(--ink2);font-size:13px}.kpi .value{font-size:32px;font-weight:600;margin-top:2px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px}
.section{margin:28px 0 10px;font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
svg{display:block;width:100%;overflow:visible}svg text{fill:var(--muted);font-size:11px}
svg .val{fill:var(--ink2)}svg .lbl{fill:var(--ink2);font-size:12px}
.legend{display:flex;gap:14px;font-size:12px;color:var(--ink2);margin:0 0 6px}.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
details{margin-top:8px;font-size:13px;color:var(--ink2)}summary{cursor:pointer;color:var(--muted)}
table{border-collapse:collapse;width:100%;margin-top:6px;font-variant-numeric:tabular-nums}td,th{padding:3px 6px;border-bottom:1px solid var(--grid);text-align:left}td:last-child,th:last-child{text-align:right}
.empty{color:var(--muted);font-size:13px;padding:12px 0}
#tip{position:fixed;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:12px;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;z-index:9}
#tip b{display:block;font-size:14px;color:var(--ink)}#tip span{color:var(--ink2)}
.hit{fill:transparent;cursor:default}.mark{pointer-events:none}.hit:hover+.mark,.hit:focus+.mark{opacity:.8}.hit:focus{outline:none}
</style></head><body><main>
<header><div><h1>🌸 __TITLE__</h1><div class="meta">__PERIOD__ · generado el __GENERATED__</div></div></header>
<section class="kpis" id="kpis"></section>
<div class="section">Música</div>
<div class="grid">
 <div class="card"><h2>Canciones por mes</h2><p class="sub">Canciones que sonaron cada mes</p><div data-chart="meses" data-kind="col"></div></div>
 <div class="card"><h2>Horas de música por mes</h2><p class="sub">Tiempo total sonando</p><div data-chart="horas_mes" data-kind="col" data-unit=" h"></div></div>
 <div class="card"><h2>¿A qué hora piden música?</h2><p class="sub">Pedidos según la hora del día</p><div data-chart="por_hora" data-kind="col"></div></div>
 <div class="card"><h2>¿Qué días?</h2><p class="sub">Pedidos según el día de la semana</p><div data-chart="por_dia" data-kind="col"></div></div>
 <div class="card"><h2>Artistas más escuchados</h2><p class="sub">Top 10</p><div data-chart="artistas" data-kind="bar"></div></div>
 <div class="card"><h2>Canciones más escuchadas</h2><p class="sub">Top 10</p><div data-chart="canciones" data-kind="bar"></div></div>
 <div class="card"><h2>DJs</h2><p class="sub">Quién pidió más canciones</p><div data-chart="djs" data-kind="bar"></div></div>
 <div class="card"><h2>Oyentes</h2><p class="sub">Horas en el canal de voz con música</p><div data-chart="oyentes" data-kind="bar" data-unit=" h"></div></div>
</div>
<div class="section">League of Legends</div>
<div class="grid">
 <div class="card"><h2>Victorias y derrotas</h2><p class="sub">Partidas de cada cuenta vinculada (en Arena, top 4 cuenta como victoria)</p><div data-chart="lol_resultados" data-kind="stack" data-series="Victorias,Derrotas"></div></div>
 <div class="card"><h2>KDA promedio</h2><p class="sub">(asesinatos + asistencias) / muertes · mínimo 3 partidas</p><div data-chart="lol_kda" data-kind="bar"></div></div>
 <div class="card"><h2>Campeones del servidor</h2><p class="sub">Los más jugados</p><div data-chart="lol_campeones" data-kind="bar"></div></div>
 <div class="card"><h2>Modos de juego</h2><p class="sub">Partidas por cola</p><div data-chart="lol_modos" data-kind="bar"></div></div>
</div>
</main><div id="tip" role="tooltip"></div>
<script>
const DATA = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const tip = document.getElementById("tip");
const fmt = v => (Math.round(v * 100) / 100).toLocaleString("es-AR");
function el(tag, attrs, parent) { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); if (parent) parent.appendChild(n); return n; }
function txt(tag, attrs, text, parent) { const n = el(tag, attrs, parent); n.textContent = text; return n; }
function showTip(ev, title, value) {
  tip.replaceChildren(); const b = document.createElement("b"); b.textContent = value; const s = document.createElement("span"); s.textContent = title;
  tip.append(b, s); tip.style.display = "block";
  const x = Math.min(ev.clientX + 14, innerWidth - tip.offsetWidth - 8); tip.style.left = x + "px"; tip.style.top = (ev.clientY + 14) + "px";
}
function hideTip() { tip.style.display = "none"; }
function nice(max) { if (max <= 0) return 1; const p = Math.pow(10, Math.floor(Math.log10(max))); const n = max / p; return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p; }
// Barra con punta redondeada (4px) y base recta
function barPath(x, y, w, h, horizontal) {
  const r = Math.min(4, horizontal ? h / 2 : w / 2, horizontal ? w : h);
  if (w <= 0 || h <= 0) return "";
  return horizontal
    ? `M${x},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${h - 2 * r}a${r},${r} 0 0 1 -${r},${r}h-${w - r}z`
    : `M${x},${y + h}v-${h - r}a${r},${r} 0 0 1 ${r},-${r}h${w - 2 * r}a${r},${r} 0 0 1 ${r},${r}v${h - r}z`;
}
function table(host, rows, cols) {
  const d = document.createElement("details"); const s = document.createElement("summary"); s.textContent = "Ver tabla"; d.appendChild(s);
  const t = document.createElement("table"); const tr = t.insertRow(); cols.forEach(c => { const th = document.createElement("th"); th.textContent = c; tr.appendChild(th); });
  rows.forEach(r => { const row = t.insertRow(); r.forEach(v => { row.insertCell().textContent = typeof v === "number" ? fmt(v) : v; }); });
  d.appendChild(t); host.appendChild(d);
}
function columns(host, rows, unit) {
  const W = Math.max(260, host.clientWidth), H = 220, L = 34, B = 22, T = 14, max = nice(Math.max(...rows.map(r => r.value), 0));
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, host);
  for (let i = 0; i <= 4; i++) { const y = T + (H - T - B) * (1 - i / 4); el("line", { x1: L, x2: W, y1: y, y2: y, stroke: i ? "var(--grid)" : "var(--axis)", "stroke-width": 1 }, svg); txt("text", { x: L - 6, y: y + 4, "text-anchor": "end" }, fmt(max * i / 4), svg); }
  const slot = (W - L) / rows.length, bw = Math.min(24, slot - 2);
  const every = Math.max(1, Math.ceil(rows.length / Math.floor(W / 44)));
  rows.forEach((r, i) => {
    const h = (H - T - B) * r.value / max, x = L + slot * i + (slot - bw) / 2, y = H - B - h;
    const hit = el("rect", { class: "hit", x: L + slot * i, y: T, width: slot, height: H - T - B, tabindex: 0 }, svg);
    el("path", { class: "mark", d: barPath(x, y, bw, h, false), fill: "var(--s1)" }, svg);
    if (i % every === 0) txt("text", { x: x + bw / 2, y: H - 6, "text-anchor": "middle" }, r.label, svg);
    const show = ev => showTip(ev, r.label, fmt(r.value) + (unit || "")); hit.addEventListener("pointermove", show); hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("focus", ev => { const b = hit.getBoundingClientRect(); show({ clientX: b.left + b.width / 2, clientY: b.top }); }); hit.addEventListener("blur", hideTip);
  });
  const peak = rows.reduce((a, b) => b.value > a.value ? b : a, rows[0]);
  if (peak && peak.value > 0) { const i = rows.indexOf(peak), x = L + slot * i + slot / 2, y = H - B - (H - T - B) * peak.value / max; txt("text", { class: "val", x, y: y - 5, "text-anchor": "middle" }, fmt(peak.value) + (unit || ""), svg); }
  table(host, rows.map(r => [r.label, r.value]), ["", "valor"]);
}
function bars(host, rows, unit, series) {
  const stacked = !!series, row = 26, W = Math.max(260, host.clientWidth), L = Math.round(Math.min(170, W * 0.36)), R = 56, H = rows.length * row + 4;
  const maxChars = Math.floor((L - 12) / 6.6);
  const totals = rows.map(r => stacked ? r.values.reduce((a, b) => a + b, 0) : r.value), max = Math.max(...totals, 0) || 1;
  if (stacked) { const lg = document.createElement("div"); lg.className = "legend"; series.forEach((s, i) => { const sp = document.createElement("span"); const sw = document.createElement("i"); sw.style.background = `var(--s${i + 1})`; sp.append(sw, document.createTextNode(s)); lg.appendChild(sp); }); host.appendChild(lg); }
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, host);
  el("line", { x1: L, x2: L, y1: 0, y2: H, stroke: "var(--axis)", "stroke-width": 1 }, svg);
  rows.forEach((r, i) => {
    const y = i * row + 4, bh = Math.min(16, row - 8), label = r.label.length > maxChars ? r.label.slice(0, maxChars - 1) + "…" : r.label;
    txt("text", { class: "lbl", x: L - 8, y: y + bh / 2 + 4, "text-anchor": "end" }, label, svg);
    const hit = el("rect", { class: "hit", x: 0, y: y - 3, width: W, height: row, tabindex: 0 }, svg);
    let x = L; const vals = stacked ? r.values : [r.value];
    vals.forEach((v, k) => {
      const w = (W - L - R) * v / max; if (w <= 0) return;
      const last = k === vals.length - 1 || vals.slice(k + 1).every(z => z <= 0);
      el("path", { class: "mark", d: last ? barPath(x, y, w, bh, true) : `M${x},${y}h${w}v${bh}h-${w}z`, fill: `var(--s${k + 1})` }, svg);
      x += w + (stacked ? 2 : 0);
    });
    txt("text", { class: "val", x: x + 6, y: y + bh / 2 + 4 }, stacked ? `${r.values[0]}–${r.values[1]}` : fmt(r.value) + (unit || ""), svg);
    const detail = stacked ? series.map((s, k) => `${s}: ${r.values[k]}`).join(" · ") : fmt(r.value) + (unit || "");
    const show = ev => showTip(ev, r.label, detail); hit.addEventListener("pointermove", show); hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("focus", ev => { const b = hit.getBoundingClientRect(); show({ clientX: b.left + L, clientY: b.top }); }); hit.addEventListener("blur", hideTip);
  });
  table(host, rows.map(r => stacked ? [r.label, ...r.values] : [r.label, r.value]), stacked ? ["", ...series] : ["", "valor"]);
}
const k = document.getElementById("kpis");
DATA.kpis.forEach(x => { const c = document.createElement("div"); c.className = "card kpi"; const l = document.createElement("div"); l.className = "label"; l.textContent = x.label; const v = document.createElement("div"); v.className = "value"; v.textContent = fmt(x.value); c.append(l, v); k.appendChild(c); });
// Se dibuja al ancho real de cada tarjeta (así el texto queda a 11-12px) y se redibuja al cambiar el tamaño.
function drawAll() {
  document.querySelectorAll("[data-chart]").forEach(host => {
    host.replaceChildren();
    const rows = DATA[host.dataset.chart] || [];
    if (!rows.length || rows.every(r => (r.values ? r.values.reduce((a, b) => a + b, 0) : r.value) === 0)) { const p = document.createElement("div"); p.className = "empty"; p.textContent = "Todavía no hay datos para esto."; host.appendChild(p); return; }
    if (host.dataset.kind === "col") columns(host, rows, host.dataset.unit);
    else bars(host, rows, host.dataset.unit, host.dataset.kind === "stack" ? host.dataset.series.split(",") : null);
  });
}
drawAll();
let resizeTimer; addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawAll, 150); });
</script></body></html>
"""


def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (TEMPLATE.replace("__TITLE__", html.escape(data["titulo"]))
            .replace("__PERIOD__", html.escape(data["periodo"]))
            .replace("__GENERATED__", html.escape(data["generado"]))
            .replace("__DATA__", payload))


def write_panel(guild: discord.Guild, month: Optional[str] = None) -> Path:
    PANEL_DIR.mkdir(exist_ok=True)
    path = PANEL_DIR / f"estadisticas_{guild.id}{'_' + month if month else ''}.html"
    path.write_text(render(build_data(guild, month)), encoding="utf-8")
    return path


class Panel(commands.Cog, name="Panel"):
    """Página con gráficos de las estadísticas del servidor."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="panel", aliases=["estadisticas", "estadísticas", "stats"],
                      help="Página con gráficos de música y League. Uso: panel [mes] (sin mes: todo el historial)")
    @commands.guild_only()
    async def panel(self, ctx: commands.Context, *, mes: str = "") -> None:
        month = wrapped.parse_month(mes) if mes.strip() else None
        if mes.strip() and month is None:
            await ctx.send("No entendí el mes 😳 Prueba con `pasado`, `septiembre` o `2026-09`.")
            return
        async with ctx.typing():
            path = write_panel(ctx.guild, month)
        await ctx.send(
            f"📊 Estadísticas de **{ctx.guild.name}** ({wrapped.month_label(month) if month else 'todo el historial'}). "
            "Descarga el archivo y ábrelo con el navegador 🌸",
            file=discord.File(path, filename=f"estadisticas_{date.today():%Y-%m-%d}.html"),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Panel(bot))
