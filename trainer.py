import os
import asyncio
import aiohttp
import requests
import json
import pandas as pd
import numpy as np
from collections import defaultdict
from datetime import datetime
from understat import Understat

# ===========================================================================
# Fuentes de datos:
#   - Football-Data.org → estructura de partidos, IDs de equipos, resultados
#   - Understat.com     → xG gratis via librería understat (aiohttp)
#   - SportAPI7         → tarjetas amarillas/rojas por partido (RapidAPI)
#
# Versión v4.1:
#   - FIX: season_id dinámico en obtener_equipos_rapid_laliga()
#          El ID ya no está hardcodeado — se obtiene primero y se pasa
#          como parámetro a obtener_equipos_rapid_laliga() y
#          obtener_eventos_laliga_rapid() para evitar fallos silenciosos.
#   - FIX: valladolid agregado a EQUIPOS_SIN_UNDERSTAT (bajó a Segunda,
#          Understat no tiene sus datos → se usa goles reales como fallback)
# ===========================================================================

API_KEY         = os.getenv("FOOTBALL_DATA_API_KEY")
RAPIDAPI_KEY    = os.getenv("RAPIDAPI_KEY")
RAPIDAPI_HOST   = "sportapi7.p.rapidapi.com"

BASE_URL = "https://api.football-data.org/v4/competitions/PD/matches?status=FINISHED"
HEADERS  = {"X-Auth-Token": API_KEY}
RAPID_HEADERS = {
    "X-RapidAPI-Key":  RAPIDAPI_KEY,
    "X-RapidAPI-Host": RAPIDAPI_HOST
}

TIME_DECAY_LAMBDA = 0.007
XG_WEIGHT         = 0.6

# Equipos que NO están en Understat LaLiga
# - levante/oviedo: Segunda División
# - valladolid: descendió temporadas recientes, sin datos en Understat
EQUIPOS_SIN_UNDERSTAT = {"levante", "oviedo", "valladolid"}

# ID de LaLiga en SportAPI7 (uniqueTournament)
LALIGA_TOURNAMENT_ID = 8

# ===========================================================================
# EXCEPCIONES DE MAPEO — nombres muy diferentes entre FD y SportAPI7
# ===========================================================================
EXCEPCIONES_FD_A_RAPID = {
    "club atlético de madrid"   : "Atlético Madrid",
    "club atletico de madrid"   : "Atlético Madrid",
    "rc celta de vigo"          : "Celta Vigo",
    "real betis balompié"       : "Real Betis",
    "real betis balompie"       : "Real Betis",
    "ca osasuna"                : "Osasuna",
    "rayo vallecano de madrid"  : "Rayo Vallecano",
    "real sociedad de fútbol"   : "Real Sociedad",
    "real sociedad de futbol"   : "Real Sociedad",
    "rcd mallorca"              : "Mallorca",
    "rcd espanyol de barcelona" : "Espanyol",
    "ud las palmas"             : "Las Palmas",
    "real valladolid cf"        : "Valladolid",
    "cd leganés"                : "Leganés",
    "cd leganes"                : "Leganés",
    "deportivo alavés"          : "Deportivo Alavés",
    "deportivo alaves"          : "Deportivo Alavés",
    "levante ud"                : "Levante UD",
    "real oviedo"               : "Real Oviedo",
    "villarreal cf"             : "Villarreal",
    "getafe cf"                 : "Getafe",
    "girona fc"                 : "Girona FC",
    "sevilla fc"                : "Sevilla",
    "valencia cf"               : "Valencia",
    "athletic club"             : "Athletic Club",
    "fc barcelona"              : "FC Barcelona",
    "real madrid cf"            : "Real Madrid",
    "ud almería"                : "Almería",
    "ud almeria"                : "Almería",
    "elche cf"                  : "Elche",
}


# ===========================================================================
# FOOTBALL-DATA — descarga temporada actual + anterior
# ===========================================================================

def descargar_partidos_football_data() -> list:
    anio = datetime.now().year
    mes  = datetime.now().month
    temporada_actual   = anio - 1 if mes < 8 else anio
    temporada_anterior = temporada_actual - 1

    todos = {}

    for season in [temporada_actual, temporada_anterior]:
        url = f"{BASE_URL}&season={season}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                partidos = resp.json().get('matches', [])
                for m in partidos:
                    todos[m['id']] = m
                print(f"   ✅ Football-Data temporada {season}: {len(partidos)} partidos descargados.")
            elif resp.status_code == 403:
                print(f"   ⚠️  Football-Data temporada {season}: sin acceso (403). Saltando.")
            else:
                print(f"   ⚠️  Football-Data temporada {season}: error {resp.status_code}. Saltando.")
        except Exception as e:
            print(f"   ⚠️  Football-Data temporada {season}: excepción {e}. Saltando.")

    resultado = list(todos.values())
    print(f"   📊 Total partidos únicos combinados: {len(resultado)}")
    return resultado


# ===========================================================================
# UNDERSTAT — carga dos temporadas para máxima cobertura
# ===========================================================================

async def _fetch_xg_dos_temporadas(temporada_actual: int):
    async with aiohttp.ClientSession() as session:
        understat = Understat(session)
        resultados = await asyncio.gather(
            understat.get_league_results("La_liga", temporada_actual),
            understat.get_league_results("La_liga", temporada_actual - 1),
            return_exceptions=True
        )
    return resultados


def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
        import nest_asyncio
        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def obtener_xg_understat() -> dict:
    xg_map = {}

    anio = datetime.now().year
    mes  = datetime.now().month
    temporada_actual = anio - 1 if mes < 8 else anio

    try:
        resultados = _run_async(_fetch_xg_dos_temporadas(temporada_actual))
    except Exception as e:
        print(f"   ⚠️  Error Understat async: {e}. Solo goles reales.")
        return xg_map

    total = 0
    for temporada_idx, partidos in enumerate(resultados):
        temporada = temporada_actual - temporada_idx
        if isinstance(partidos, Exception):
            print(f"   ⚠️  Error Understat temporada {temporada}: {partidos}")
            continue

        for p in partidos:
            xg_data = p.get('xG', {})
            if not xg_data:
                continue
            try:
                xg_h = float(xg_data.get('h') or 0)
                xg_a = float(xg_data.get('a') or 0)
            except (TypeError, ValueError):
                continue
            if not (0 <= xg_h <= 8 and 0 <= xg_a <= 8):
                continue

            home      = p.get('h', {}).get('title', '').strip().lower()
            away      = p.get('a', {}).get('title', '').strip().lower()
            fecha_raw = p.get('datetime', '')
            fecha_iso = fecha_raw[:10] if fecha_raw else ''

            if home and away and fecha_iso:
                xg_map[(fecha_iso, home, away)] = (xg_h, xg_a)
                xg_map.setdefault(('', home, away), (xg_h, xg_a))
                total += 1

    print(f"   ✅ Understat ({temporada_actual} + {temporada_actual-1}): {total} partidos con xG cargados.")
    return xg_map


# ===========================================================================
# NORMALIZACIÓN DE NOMBRES (Football-Data → Understat)
# ===========================================================================

NOMBRE_FD_A_US = {
    "real madrid cf"                : "real madrid",
    "fc barcelona"                  : "barcelona",
    "club atlético de madrid"       : "atletico madrid",
    "club atletico de madrid"       : "atletico madrid",
    "athletic club"                 : "athletic club",
    "real sociedad de fútbol"       : "real sociedad",
    "real sociedad de futbol"       : "real sociedad",
    "villarreal cf"                 : "villarreal",
    "real betis balompié"           : "real betis",
    "real betis balompie"           : "real betis",
    "sevilla fc"                    : "sevilla",
    "valencia cf"                   : "valencia",
    "rayo vallecano de madrid"      : "rayo vallecano",
    "getafe cf"                     : "getafe",
    "girona fc"                     : "girona",
    "deportivo alavés"              : "alaves",
    "deportivo alaves"              : "alaves",
    "ca osasuna"                    : "osasuna",
    "rc celta de vigo"              : "celta vigo",
    "rcd mallorca"                  : "mallorca",
    "rcd espanyol de barcelona"     : "espanyol",
    "ud las palmas"                 : "las palmas",
    "real valladolid cf"            : "valladolid",
    "cd leganés"                    : "leganes",
    "cd leganes"                    : "leganes",
    "elche cf"                      : "elche",
    "levante ud"                    : "levante",
    "real oviedo"                   : "oviedo",
    "atletico de madrid"            : "atletico madrid",
    "ud girona fc"                  : "girona",
    "ud almería"                    : "almeria",
    "ud almeria"                    : "almeria",
    "cd espanyol de barcelona"      : "espanyol",
    "real racing club de santander" : "racing santander",
}

def normalizar_nombre(nombre_fd: str) -> str:
    key = nombre_fd.strip().lower()
    return NOMBRE_FD_A_US.get(key, key)


# ===========================================================================
# SPORTAPI7 — season ID dinámico
# ===========================================================================

def obtener_season_id_laliga() -> int:
    """
    Obtiene el seasonId actual de LaLiga desde SportAPI7.
    Fallback al ID conocido si falla.
    """
    SEASON_ID_FALLBACK = 61643  # LaLiga 25/26

    if not RAPIDAPI_KEY:
        return SEASON_ID_FALLBACK

    try:
        url = f"https://{RAPIDAPI_HOST}/api/v1/unique-tournament/{LALIGA_TOURNAMENT_ID}/seasons"
        r   = requests.get(url, headers=RAPID_HEADERS, timeout=10)
        if r.status_code == 200:
            seasons = r.json().get("seasons", [])
            if seasons:
                season_id = seasons[0].get("id", SEASON_ID_FALLBACK)
                print(f"   ✅ SportAPI7 season ID LaLiga: {season_id} ({seasons[0].get('name', '')})")
                return season_id
    except Exception as e:
        print(f"   ⚠️  Error obteniendo season ID: {e}. Usando fallback {SEASON_ID_FALLBACK}.")

    return SEASON_ID_FALLBACK


# ===========================================================================
# SPORTAPI7 — mapeo dinámico Football-Data → SportAPI7
# ===========================================================================

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


def construir_mapeo_rapid(equipos_fd: list, equipos_rapid: list) -> dict:
    mapeo     = {}
    sin_match = []

    for nombre_fd in equipos_fd:
        key_fd = nombre_fd.strip().lower()

        if key_fd in EXCEPCIONES_FD_A_RAPID:
            mapeo[key_fd] = EXCEPCIONES_FD_A_RAPID[key_fd]
            continue

        mejor_score  = 0.0
        mejor_nombre = None
        for nombre_r in equipos_rapid:
            score = _similitud(nombre_fd, nombre_r)
            if score > mejor_score:
                mejor_score  = score
                mejor_nombre = nombre_r

        if mejor_score >= 0.5 and mejor_nombre:
            mapeo[key_fd] = mejor_nombre
        else:
            sin_match.append(nombre_fd)
            mapeo[key_fd] = nombre_fd

    if sin_match:
        print(f"   ⚠️  Sin match en SportAPI7 para: {sin_match}")
        print(f"      → Pueden haber descendido o cambiar nombre. Revisa en agosto.")

    return mapeo


def obtener_equipos_rapid_laliga(season_id: int) -> list:
    """
    Obtiene la lista de equipos de LaLiga desde SportAPI7.
    Recibe season_id dinámico — ya no está hardcodeado.
    """
    if not RAPIDAPI_KEY:
        return []

    equipos = set()

    try:
        url = f"https://{RAPIDAPI_HOST}/api/v1/unique-tournament/{LALIGA_TOURNAMENT_ID}/season/{season_id}/standings/total"
        r   = requests.get(url, headers=RAPID_HEADERS, timeout=15)
        if r.status_code == 200:
            data = r.json()
            rows = data.get("standings", [{}])[0].get("rows", [])
            for row in rows:
                nombre = row.get("team", {}).get("name", "")
                if nombre:
                    equipos.add(nombre)
            if equipos:
                print(f"   ✅ SportAPI7 standings: {len(equipos)} equipos obtenidos (season {season_id}).")
                return list(equipos)
        else:
            print(f"   ⚠️  SportAPI7 standings HTTP {r.status_code} para season {season_id}.")
    except Exception as e:
        print(f"   ⚠️  Error obteniendo standings SportAPI7: {e}")

    # Fallback: buscar en partidos del día de hoy
    try:
        fecha_hoy = datetime.now().strftime("%Y-%m-%d")
        url = f"https://{RAPIDAPI_HOST}/api/v1/sport/football/scheduled-events/{fecha_hoy}"
        r   = requests.get(url, headers=RAPID_HEADERS, timeout=15)
        if r.status_code == 200:
            events = r.json().get("events", [])
            for ev in events:
                torneo = ev.get("tournament", {})
                unique = torneo.get("uniqueTournament", {})
                if unique.get("id") == LALIGA_TOURNAMENT_ID:
                    equipos.add(ev.get("homeTeam", {}).get("name", ""))
                    equipos.add(ev.get("awayTeam", {}).get("name", ""))
            equipos.discard("")
            print(f"   ✅ SportAPI7 fallback scheduled: {len(equipos)} equipos obtenidos.")
    except Exception as e:
        print(f"   ⚠️  Error fallback SportAPI7: {e}")

    return list(equipos)


# ===========================================================================
# SPORTAPI7 — descarga tarjetas por equipo desde incidents
# ===========================================================================

def obtener_eventos_laliga_rapid(season_id: int) -> list:
    if not RAPIDAPI_KEY:
        return []

    eventos = []
    try:
        for ronda in range(33, 38):
            url = f"https://{RAPIDAPI_HOST}/api/v1/unique-tournament/{LALIGA_TOURNAMENT_ID}/season/{season_id}/events/round/{ronda}"
            r   = requests.get(url, headers=RAPID_HEADERS, timeout=10)
            if r.status_code == 200:
                evs = r.json().get("events", [])
                for ev in evs:
                    if ev.get("status", {}).get("type") == "finished":
                        eventos.append({
                            "id":   ev["id"],
                            "home": ev.get("homeTeam", {}).get("name", ""),
                            "away": ev.get("awayTeam", {}).get("name", "")
                        })
            import time; time.sleep(0.3)

        print(f"   ✅ SportAPI7 eventos LaLiga: {len(eventos)} partidos terminados encontrados.")
    except Exception as e:
        print(f"   ⚠️  Error obteniendo eventos LaLiga: {e}")

    return eventos


def obtener_tarjetas_por_partido(event_id: int) -> dict:
    if not RAPIDAPI_KEY:
        return {}
    try:
        url = f"https://{RAPIDAPI_HOST}/api/v1/event/{event_id}/incidents"
        r   = requests.get(url, headers=RAPID_HEADERS, timeout=10)
        print(f"   [DEBUG] event {event_id} → HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"   [DEBUG] response: {r.text[:300]}")
            return {}
        data      = r.json()
        incidents = data.get("incidents", [])
        print(f"   [DEBUG] incidents recibidos: {len(incidents)}")
        if incidents:
            print(f"   [DEBUG] ejemplo: {json.dumps(incidents[0], ensure_ascii=False)[:300]}")
        tarjetas = defaultdict(lambda: {"amarillas": 0, "rojas": 0})
        for inc in incidents:
            tipo = str(inc.get("incidentType", "")).lower()
            if "card" not in tipo:
                continue
            color     = str(inc.get("incidentClass", "")).lower()
            nombre_eq = inc.get("team", {}).get("name", "")
            if not nombre_eq:
                continue
            if "yellow" in color or "yellow" in tipo:
                tarjetas[nombre_eq]["amarillas"] += 1
            elif "red" in color or "red" in tipo:
                tarjetas[nombre_eq]["rojas"] += 1
        return dict(tarjetas)
    except Exception as e:
        print(f"   [DEBUG] excepción event {event_id}: {e}")
        return {}


def calcular_tarjetas_promedio(equipos_fd: list, mapeo_rapid: dict, eventos: list) -> dict:
    acum = defaultdict(lambda: {"amarillas": [], "rojas": []})

    total_eventos    = len(eventos)
    eventos_ok       = 0
    eventos_sin_data = 0

    print(f"   Procesando incidents de {total_eventos} partidos...")

    import time
    for i, ev in enumerate(eventos):
        tarjetas = obtener_tarjetas_por_partido(ev["id"])
        if tarjetas:
            eventos_ok += 1
            for nombre_eq, datos in tarjetas.items():
                acum[nombre_eq]["amarillas"].append(datos["amarillas"])
                acum[nombre_eq]["rojas"].append(datos["rojas"])
        else:
            eventos_sin_data += 1

        if (i + 1) % 5 == 0:
            time.sleep(0.5)

    print(f"   ✅ Incidents procesados: {eventos_ok} OK | {eventos_sin_data} sin data")

    resultado = {}
    for nombre_fd in equipos_fd:
        key_fd     = nombre_fd.strip().lower()
        nombre_rap = mapeo_rapid.get(key_fd, nombre_fd)

        datos_rap = acum.get(nombre_rap)
        if not datos_rap or not datos_rap["amarillas"]:
            for k, v in acum.items():
                if _similitud(nombre_rap, k) >= 0.6 and v["amarillas"]:
                    datos_rap = v
                    break

        if datos_rap and datos_rap["amarillas"]:
            resultado[nombre_fd] = {
                "avg_amarillas":       round(float(np.mean(datos_rap["amarillas"])), 2),
                "avg_rojas":           round(float(np.mean(datos_rap["rojas"])), 2),
                "partidos_analizados": len(datos_rap["amarillas"])
            }
        else:
            resultado[nombre_fd] = {
                "avg_amarillas":       2.1,
                "avg_rojas":           0.1,
                "partidos_analizados": 0
            }

    return resultado


# ===========================================================================
# H2H
# ===========================================================================

def construir_h2h(matches_raw: list, xg_understat: dict) -> dict:
    pares = defaultdict(list)

    for m in matches_raw:
        if not (m.get('score') and m['score'].get('fullTime')):
            continue
        winner = m['score'].get('winner')
        if not winner:
            continue

        h_id    = m['homeTeam']['id']
        a_id    = m['awayTeam']['id']
        h_name  = m['homeTeam']['name']
        a_name  = m['awayTeam']['name']
        goles_h = m['score']['fullTime']['home']
        goles_a = m['score']['fullTime']['away']
        fecha   = m['utcDate'][:10]

        h_us   = normalizar_nombre(h_name)
        a_us   = normalizar_nombre(a_name)
        xg_dat = xg_understat.get((fecha, h_us, a_us)) or xg_understat.get(('', h_us, a_us))
        xg_h   = round(xg_dat[0], 2) if xg_dat else None
        xg_a   = round(xg_dat[1], 2) if xg_dat else None

        pares[(h_id, a_id)].append({
            "fecha":   fecha,
            "goles_h": goles_h,
            "goles_a": goles_a,
            "xg_h":    xg_h,
            "xg_a":    xg_a,
            "winner":  winner
        })

    h2h_out = {}
    for (h_id, a_id), partidos in pares.items():
        partidos_ord      = sorted(partidos, key=lambda p: p["fecha"], reverse=True)[:6]
        temporadas_con_xg = sum(1 for p in partidos_ord if p["xg_h"] is not None)
        h2h_out[f"{h_id}_{a_id}"] = {
            "partidos":          partidos_ord,
            "temporadas_con_xg": temporadas_con_xg
        }

    return h2h_out


# ===========================================================================
# ENTRENAMIENTO PRINCIPAL
# ===========================================================================

def train_spain():
    if not API_KEY:
        print("❌ ERROR: No se encontró FOOTBALL_DATA_API_KEY.")
        return

    usar_rapid = bool(RAPIDAPI_KEY)
    if not usar_rapid:
        print("⚠️  RAPIDAPI_KEY no encontrada. Se omitirán las tarjetas.")

    try:
        print(f"Consultando LaLiga | λ={TIME_DECAY_LAMBDA} | xG weight={XG_WEIGHT}...")

        # --- Understat xG ---
        print("Descargando xG desde Understat (2 temporadas)...")
        xg_understat = obtener_xg_understat()

        # --- Football-Data: temporada actual + anterior ---
        print("Descargando partidos desde Football-Data (2 temporadas)...")
        matches = descargar_partidos_football_data()

        if not matches:
            print("⚠️ No hay partidos terminados.")
            return

        goles = []
        team_ids = {}
        partidos_con_xg        = 0
        partidos_sin_xg        = 0
        partidos_sin_understat = 0
        equipos_sin_xg         = set()

        for m in matches:
            if not (m.get('score') and m['score'].get('fullTime')):
                continue

            home_name = m['homeTeam']['name']
            away_name = m['awayTeam']['name']
            team_ids[home_name] = m['homeTeam']['id']
            team_ids[away_name] = m['awayTeam']['id']

            goals_h = m['score']['fullTime']['home']
            goals_a = m['score']['fullTime']['away']

            home_us = normalizar_nombre(home_name)
            away_us = normalizar_nombre(away_name)

            if home_us in EQUIPOS_SIN_UNDERSTAT or away_us in EQUIPOS_SIN_UNDERSTAT:
                valor_h = float(goals_h)
                valor_a = float(goals_a)
                partidos_sin_understat += 1
            else:
                fecha_iso = m['utcDate'][:10]
                xg_data   = xg_understat.get((fecha_iso, home_us, away_us))

                if not xg_data:
                    xg_data = xg_understat.get(('', home_us, away_us))

                if xg_data:
                    xg_h, xg_a = xg_data
                    valor_h = (xg_h * XG_WEIGHT) + (goals_h * (1 - XG_WEIGHT))
                    valor_a = (xg_a * XG_WEIGHT) + (goals_a * (1 - XG_WEIGHT))
                    partidos_con_xg += 1
                else:
                    valor_h = float(goals_h)
                    valor_a = float(goals_a)
                    partidos_sin_xg += 1
                    equipos_sin_xg.add(f"{fecha_iso} | {home_name} vs {away_name} (US: {home_us} vs {away_us})")

            goles.append({
                'home':    home_name,
                'away':    away_name,
                'goals_h': valor_h,
                'goals_a': valor_a,
                'date':    m['utcDate']
            })

        if equipos_sin_xg:
            print(f"   🔍 Partidos sin xG (revisar mapeo):")
            for e in sorted(equipos_sin_xg):
                print(f"      - {e}")

        df = pd.DataFrame(goles)
        df['date']       = pd.to_datetime(df['date'])
        max_date         = df['date'].max()
        df['days_since'] = (max_date - df['date']).dt.days
        df['weight']     = np.exp(-TIME_DECAY_LAMBDA * df['days_since'])

        avg_h = np.average(df['goals_h'], weights=df['weight'])
        avg_a = np.average(df['goals_a'], weights=df['weight'])

        teams_stats = {}
        equipos_fd  = []
        for team in pd.unique(df[['home', 'away']].values.ravel()):
            equipos_fd.append(team)
            h_df = df[df['home'] == team]
            a_df = df[df['away'] == team]

            att_h = np.average(h_df['goals_h'], weights=h_df['weight']) / avg_h if not h_df.empty else 1.0
            def_h = np.average(h_df['goals_a'], weights=h_df['weight']) / avg_a if not h_df.empty else 1.0
            att_a = np.average(a_df['goals_a'], weights=a_df['weight']) / avg_a if not a_df.empty else 1.0
            def_a = np.average(a_df['goals_h'], weights=a_df['weight']) / avg_h if not a_df.empty else 1.0

            teams_stats[team] = {
                "id_api": int(team_ids.get(team, 0)),
                "att_h":  float(att_h),
                "def_h":  float(def_h),
                "att_a":  float(att_a),
                "def_a":  float(def_a)
            }

        # --- SportAPI7: tarjetas promedio ---
        # FIX: obtener season_id dinámico PRIMERO y pasarlo a ambas funciones
        tarjetas_data = {}
        if usar_rapid:
            print("Obteniendo tarjetas desde SportAPI7...")

            season_id     = obtener_season_id_laliga()
            equipos_rapid = obtener_equipos_rapid_laliga(season_id)

            if equipos_rapid:
                mapeo_rapid = construir_mapeo_rapid(equipos_fd, equipos_rapid)
                print(f"   ✅ Mapeo construido para {len(mapeo_rapid)} equipos.")

                eventos = obtener_eventos_laliga_rapid(season_id)

                if eventos:
                    tarjetas_data = calcular_tarjetas_promedio(equipos_fd, mapeo_rapid, eventos)
                    print(f"   ✅ Tarjetas calculadas para {len(tarjetas_data)} equipos.")
                else:
                    print("   ⚠️  Sin eventos de SportAPI7. Tarjetas omitidas.")
            else:
                print("   ⚠️  No se obtuvieron equipos de SportAPI7. Tarjetas omitidas.")

        # Agregar tarjetas a teams_stats
        for team in teams_stats:
            if team in tarjetas_data:
                teams_stats[team]["tarjetas"] = tarjetas_data[team]
            else:
                teams_stats[team]["tarjetas"] = {
                    "avg_amarillas":       2.1,
                    "avg_rojas":           0.1,
                    "partidos_analizados": 0
                }

        # H2H
        print("Construyendo sección H2H...")
        h2h_data = construir_h2h(matches, xg_understat)
        print(f"   ✅ H2H: {len(h2h_data)} pares de equipos registrados.")

        output = {
            "LaLiga": {
                "averages": {"league_home": float(avg_h), "league_away": float(avg_a)},
                "teams":    teams_stats,
                "h2h":      h2h_data
            },
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "meta": {
                "partidos_totales":        len(goles),
                "partidos_con_xg":         partidos_con_xg,
                "partidos_sin_xg":         partidos_sin_xg,
                "partidos_sin_understat":  partidos_sin_understat,
                "xg_weight":               XG_WEIGHT,
                "time_decay_lambda":       TIME_DECAY_LAMBDA,
                "pares_h2h":               len(h2h_data),
                "tarjetas_con_data":       sum(1 for t in teams_stats.values() if t["tarjetas"]["partidos_analizados"] > 0)
            }
        }

        with open('modelo_poisson.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=4, ensure_ascii=False)

        print(f"\n✅ modelo_poisson.json actualizado (v4.1).")
        print(f"   Equipos:               {len(teams_stats)}")
        print(f"   Partidos totales:      {len(goles)}")
        print(f"   Con xG:                {partidos_con_xg}")
        print(f"   Sin xG (mapeo):        {partidos_sin_xg}")
        print(f"   Sin Understat:         {partidos_sin_understat} (Levante/Oviedo/Valladolid — normal)")
        print(f"   Pares H2H:             {len(h2h_data)}")
        print(f"   Equipos con tarjetas:  {output['meta']['tarjetas_con_data']}")

    except Exception as e:
        print(f"❌ Error crítico: {e}")
        raise


if __name__ == "__main__":
    train_spain()
