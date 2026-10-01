"""Preguntas de esports: reconocer equipos y nombres de jugadores, y dejar de lado los modelos viejos de Gemini."""

import esports_datos as e
import ia_gemini
import opgg


def test_nombres_y_equipos_de_la_pregunta():
    assert e.candidate_names("cuando vuelve a jugar josedeodo en la lcs?") == ["josedeodo"]
    assert e.teams_in("contra quién juega Team Liquid?") == ["Team Liquid"]
    assert e.teams_in("y Leviatán?") == ["Leviatán"]
    assert e.teams_in('{"team": "Team Liquid", "region": "NA"}') == ["Team Liquid"]  # de la ficha de OP.GG
    assert opgg.league_in("cuando juega josedeodo en la lcs?") == "lcs"
    assert opgg.is_team_or_league("liquid") and not opgg.is_team_or_league("josedeodo")


def test_gemini_viejo_no_se_usa():
    assert ia_gemini._too_old("gemini-2.5-flash") and ia_gemini._too_old("gemini-2.5-flash-lite")
    assert not ia_gemini._too_old("gemini-3.5-flash-lite")
