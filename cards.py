import os
import json
import requests
import numpy as np
from collections import defaultdict
from datetime import datetime
import time

RAPIDAPI_KEY  = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST = "sportapi7.p.rapidapi.com"
RAPID_HEADERS = {
    "X-RapidAPI-Key":  RAPIDAPI_KEY or "69d26833dbmsh4d90d8b7f27f1bfp1c2c54jsn373a7e1afc6b",
    "X-RapidAPI-Host": RAPIDAPI_HOST
}

LALIGA_TOURNAMENT_ID  = 8
SEASON_ID_FALLBACK    = 61643
MODELO_PATH           = "modelo_poisson.json"


def _similitud(a: str, b: str) -> float:
    palabras_a = set(a.lower().split())
    palabras_b = set(b.lower().split())
    stopwords  = {"fc", "cf", "ud", "cd", "rc", "rcd", "ca", "de", "la", "el", "los", "club"}
    palabras_a -= stopwords
    palabras_b -= stopwords
    if not palabras_a or not palabras_b:
        return 0.0
    comunes = palabras_a & palabras_b
    return len(comunes) / max(len(palabras_a), len(palabras_b))


def obtener_season_id() -> int:
    try:
        url = f"https://{RAPIDAPI_HOST}/api/v1/unique-tournament/{LALIGA_TOURNAMENT_ID}/seasons"
        r   = requests.get(url, headers=RAPID_HEADERS, timeout=10)
        if r.status_code == 200:
            seasons = r.json().get("seasons", [])
            if seasons:
                sid = seasons[0].get("id", SEASON_ID_FALLBACK)
                print(f"   ✅ Season ID: {sid} ({seasons[0].get('name', '')})")
                return sid
        print(f"   ⚠️  HTTP {r.status_code} obteniendo season. Usando fallback.")
    except Exception as e:
        print(f"   ⚠️  Error season ID: {e}")
    return SEASON_ID_FALLBACK


def obtener_equipos(season_id: int) -> list:
    time.sleep(2)
    try:
        url = f"https://{RAPIDAPI_HOST}/api/v1/unique-tournament/{LALIGA_TOURNAMENT_ID}/season/{season_id}/standings/total"
        r   = requests.get(url, headers=RAPID_HEADERS, timeout=15)
        if r.status_code == 200:
            rows    = r.json().get("standings", [{}])[0].get("rows", [])
            equipos = [row.get("team", {}).get("name", "") for row in rows if row.get("team", {}).get("name")]
            print(f"   ✅ {len(equipos)} equipos obtenidos.")
            return equipos
        print(f"   ⚠️  HTTP {r.status_code} obteniendo equipos.")
    except Exception as e:
        print(f"   ⚠️  Error equipos: {e}")
    return []


def obtener_eventos(season_id: int) -> list:
    eventos = []
    # Últimas 10 rondas para datos recientes y confiables
    for ronda in range(28, 38):
        try:
            url = f"https://{RAPIDAPI_HOST}/api/v1/unique-tournament/{LALIGA_TOURNAMENT_ID}/season/{season_id}/events/round/{ronda}"
            r   = requests.get(url, headers=RAPID_HEADERS, timeout=10)
            if r.status_code == 429:
                print(f"   ⚠️  Rate limit (429) — abortando.")
                break
            if r.status_code == 200:
                for ev in r.json().get("events", []):
                    if ev.get("status", {}).get("type") == "finished":
                        eventos.append({
                            "id":   ev["id"],
                            "home": ev.get("homeTeam", {}).get("name", ""),
                            "away": ev.get("awayTeam", {}).get("name", "")
                        })
            time.sleep(1.5)
        except Exception as e:
            print(f"   ⚠️  Error ronda {ronda}: {e}")
    print(f"   ✅ {len(eventos)} partidos terminados encontrados.")
    return eventos


def obtener_tarjetas_partido(event_id: int, home: str, away: str) -> dict:
    try:
        url = f"https://{RAPIDAPI_HOST}/api/v1/event/{event_id}/incidents"
        r   = requests.get(url, headers=RAPID_HEADERS, timeout=10)
        if r.status_code == 429:
            print(f"   ⚠️  Rate limit (429) — abortando incidents.")
            return None
        if r.status_code != 200:
            return {}

        incidents = r.json().get("incidents", [])
        tarjetas  = defaultdict(lambda: {"amarillas": 0, "rojas": 0})

        for inc in incidents:
            if inc.get("incidentType") != "card":
                continue
            card_type      = str(inc.get("cardType", "")).lower()
            incident_class = str(inc.get("incidentClass", "")).lower()
            is_home        = inc.get("isHome")

            nombre_eq = home if is_home is True else (away if is_home is False else inc.get("team", {}).get("name", ""))
            if not nombre_eq:
                continue

            if "yellowred" in card_type or "yellowred" in incident_class:
                tarjetas[nombre_eq]["rojas"] += 1
            elif "yellow" in card_type or "yellow" in incident_class:
                tarjetas[nombre_eq]["amarillas"] += 1
            elif "red" in card_type or "red" in incident_class:
                tarjetas[nombre_eq]["rojas"] += 1

        return dict(tarjetas)
    except Exception as e:
        print(f"   ⚠️  Error incident {event_id}: {e}")
        return {}


def calcular_tarjetas(equipos_rapid: list, eventos: list) -> dict:
    acum = defaultdict(lambda: {"amarillas": [], "rojas": []})
    ok = 0
    sin_data = 0

    print(f"   Procesando incidents de {len(eventos)} partidos...")
    for i, ev in enumerate(eventos):
        tarjetas = obtener_tarjetas_partido(ev["id"], ev["home"], ev["away"])
        if tarjetas is None:
            print(f"   ⚠️  Abortando — cuota agotada.")
            break
        if tarjetas:
            ok += 1
            for nombre_eq, datos in tarjetas.items():
                acum[nombre_eq]["amarillas"].append(datos["amarillas"])
                acum[nombre_eq]["rojas"].append(datos["rojas"])
        else:
            sin_data += 1
        time.sleep(1) if (i + 1) % 5 != 0 else time.sleep(2)

    print(f"   ✅ Incidents: {ok} OK | {sin_data} sin data")

    resultado = {}
    for equipo in equipos_rapid:
        datos = acum.get(equipo)
        if not datos or not datos["amarillas"]:
            # intento por similitud
            for k, v in acum.items():
                if _similitud(equipo, k) >= 0.6 and v["amarillas"]:
                    datos = v
                    break
        if datos and datos["amarillas"]:
            resultado[equipo] = {
                "avg_amarillas":       round(float(np.mean(datos["amarillas"])), 2),
                "avg_rojas":           round(float(np.mean(datos["rojas"])), 2),
                "partidos_analizados": len(datos["amarillas"])
            }
        else:
            resultado[equipo] = {"avg_amarillas": 2.1, "avg_rojas": 0.1, "partidos_analizados": 0}

    return resultado


def actualizar_modelo(tarjetas_rapid: dict, equipos_rapid: list):
    if not os.path.exists(MODELO_PATH):
        print(f"❌ {MODELO_PATH} no encontrado.")
        return

    with open(MODELO_PATH, "r", encoding="utf-8") as f:
        modelo = json.load(f)

    liga  = next(iter(modelo))
    teams = modelo[liga]["teams"]
    actualizados = 0

    for nombre_fd, stats in teams.items():
        key_fd = nombre_fd.strip().lower()
        # buscar match en equipos_rapid
        mejor_nombre = None
        mejor_score  = 0.0
        for nombre_r in equipos_rapid:
            score = _similitud(nombre_fd, nombre_r)
            if score > mejor_score:
                mejor_score  = score
                mejor_nombre = nombre_r

        tarjeta_data = None
        if mejor_nombre and mejor_score >= 0.4:
            tarjeta_data = tarjetas_rapid.get(mejor_nombre)

        if tarjeta_data and tarjeta_data["partidos_analizados"] > 0:
            stats["tarjetas"] = tarjeta_data
            actualizados += 1
        elif "tarjetas" not in stats:
            stats["tarjetas"] = {"avg_amarillas": 2.1, "avg_rojas": 0.1, "partidos_analizados": 0}

    modelo["last_update_tarjetas"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(MODELO_PATH, "w", encoding="utf-8") as f:
        json.dump(modelo, f, indent=4, ensure_ascii=False)

    print(f"✅ modelo_poisson.json actualizado — {actualizados} equipos con tarjetas reales.")


def main():
    if not RAPIDAPI_KEY:
        print("❌ RAPIDAPI_KEY no encontrada.")
        return

    print("🃏 Actualizando tarjetas LaLiga...")
    season_id      = obtener_season_id()
    equipos_rapid  = obtener_equipos(season_id)
    if not equipos_rapid:
        print("❌ Sin equipos. Abortando.")
        return

    eventos        = obtener_eventos(season_id)
    if not eventos:
        print("❌ Sin eventos. Abortando.")
        return

    tarjetas_rapid = calcular_tarjetas(equipos_rapid, eventos)
    actualizar_modelo(tarjetas_rapid, equipos_rapid)

    equipos_con_data = sum(1 for t in tarjetas_rapid.values() if t["partidos_analizados"] > 0)
    print(f"\n📊 Resumen:")
    print(f"   Equipos con tarjetas reales: {equipos_con_data}/{len(equipos_rapid)}")
    print(f"   Partidos analizados:         {len(eventos)}")


if __name__ == "__main__":
    main()
