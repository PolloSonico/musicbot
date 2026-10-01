"""Ayudas para las preguntas de esports de League: reconocer equipos y posibles nombres de jugadores
en una pregunta ("¿cuándo vuelve a jugar Josedeodo en la LCS?").

Los datos en sí salen de OP.GG (opgg.py) y de la búsqueda en internet (busqueda.py). Antes se usaba
Leaguepedia, pero su API bloquea las consultas sin cuenta ("rate limit"), así que se quitó.
"""

import re

MAX_NAMES = 8

# Equipos que se suelen nombrar -> nombre completo.
TEAMS = {
    "team liquid": "Team Liquid", "liquid": "Team Liquid", "cloud9": "Cloud9", "cloud 9": "Cloud9", "c9": "Cloud9",
    "flyquest": "FlyQuest", "100 thieves": "100 Thieves", "dignitas": "Dignitas", "shopify rebellion": "Shopify Rebellion",
    "lyon gaming": "LYON", "disguised": "Disguised", "t1": "T1", "gen.g": "Gen.G", "geng": "Gen.G",
    "hanwha": "Hanwha Life Esports", "hle": "Hanwha Life Esports", "kt rolster": "KT Rolster", "dplus": "Dplus KIA",
    "drx": "DRX", "nongshim": "Nongshim RedForce", "fearx": "BNK FEARX", "g2": "G2 Esports", "fnatic": "Fnatic",
    "movistar koi": "Movistar KOI", "bilibili": "Bilibili Gaming", "blg": "Bilibili Gaming", "jdg": "JD Gaming",
    "top esports": "Top Esports", "weibo": "Weibo Gaming", "anyone's legend": "Anyone's Legend",
    "isurus": "Isurus", "leviatán": "Leviatán", "leviatan": "Leviatán", "estral": "Estral Esports",
    "loud": "LOUD", "pain gaming": "paiN Gaming", "furia": "FURIA", "vivo keyd": "Vivo Keyd Stars",
    "red canids": "RED Canids", "fluxo": "Fluxo W7M",
}

# Palabras comunes que no son nombres de jugadores (para no consultar de más).
STOP = set("""
a al algo alguien and ante como con contra cual cuál cuando cuándo cuanto cuánto de decime decir del dia día dias
días donde dónde el ella ellos en entre equipo es esta está este esto fecha final finde for hoy juega juegan jugar
jugo jugó juego la las le lo los lol league mañana me mi mis muy nos o otra otro para partido partidos pero por
que qué quien quién se si sí sobre su sus te temporada the tiene to torneo tu un una uno vez vs vuelve y ya yo
lcs lck lpl lec lta lcp cblol lla nacl ldl ljl msi worlds mundial liga esports competitivo pro player jugador
jugadores proximo próximo proxima próxima cuando ahora todavia todavía sigue va vuelva hora horario lillia
""".split())


def candidate_names(text: str) -> list[str]:
    """Palabras que podrían ser nombres de jugadores ("Josedeodo", "Faker"...)."""
    words = re.findall(r"[A-Za-zÀ-ÿ0-9][\w.'-]{2,20}", text)
    seen, out = set(), []
    for w in words:
        low = w.lower().strip(".'-")
        if low in STOP or low.isdigit() or low in seen:
            continue
        seen.add(low)
        out.append(w.strip(".'-"))
    return out[:MAX_NAMES]


def teams_in(text: str) -> list[str]:
    low = f" {text.lower()} "
    found = []
    for name, page in TEAMS.items():
        if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", low) and page not in found:
            found.append(page)
    return found[:3]
