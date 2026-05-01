import os
import asyncio
import aiohttp
import requests
import json
import pandas as pd
import numpy as np
from datetime import datetime
from understat import Understat

# ===========================================================================
# Fuentes de datos:
#   - Football-Data.org → estructura de partidos, IDs de equipos, resultados
#   - Understat.com     → xG gratis via librería understat (aiohttp)
#
# Correcciones v2:
#   1. Carga temporada actual + anterior para cubrir partidos de inicio de temporada
#   2. Cruce por fecha+equipos en lugar de solo nombres (evita falsos negativos)
#   3. asyncio seguro: usa nest_asyncio si hay event loop activo (Railway/GitHub Actions)
#   4. Levante UD y Real Oviedo no existen en Understat LaLiga → usan solo goles reales
# ===========================================================================

API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
URL = "https://api.football-data.org/v4/competitions/PD/matches?status=FINISHED"
HEADERS = {"X-Auth-Token": API_KEY}

TIME_DECAY_LAMBDA = 0.007
XG_WEIGHT = 0.6

# Equipos que NO están en Understat LaLiga (Segunda División ascendidos, etc.)
# Para estos se usan solo goles reales sin penalización
EQUIPOS_SIN_UNDERSTAT = {"levante", "oviedo"}


# ===========================================================================
# UNDERSTAT — carga dos temporadas para máxima cobertura
# ===========================================================================

async def _fetch_xg_dos_temporadas(temporada_actual: int):
    """Descarga partidos de la temporada actual y la anterior en paralelo."""
    async with aiohttp.ClientSession() as session:
        understat = Understat(session)
        resultados = await asyncio.gather(
            understat.get_league_results("La_liga", temporada_actual),
            understat.get_league_results("La_liga", temporada_actual - 1),
            return_exceptions=True
        )
    return resultados


def _run_async(coro):
    """Ejecuta una coroutine de forma segura en cualquier entorno."""
    try:
        loop = asyncio.get_running_loop()
        # Si hay loop activo (Railway, Jupyter, etc.) usar nest_asyncio
        import nest_asyncio
        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    except RuntimeError:
        # No hay loop activo — comportamiento normal
        return asyncio.run(coro)


def obtener_xg_understat() -> dict:
    """
    Obtiene xG de LaLiga desde Understat para dos temporadas.
    Clave del dict: (fecha_iso, home_norm, away_norm) → (xg_h, xg_a)
    Esto permite cruce exacto por fecha + equipos, eliminando falsos negativos.
    """
    xg_map = {}

    anio = datetime.now().year
    mes = datetime.now().month
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
            print(f"   ⚠️  Error temporada {temporada}: {partidos}")
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

            home = p.get('h', {}).get('title', '').strip().lower()
            away = p.get('a', {}).get('title', '').strip().lower()
            fecha_raw = p.get('datetime', '')
            fecha_iso = fecha_raw[:10] if fecha_raw else ''  # YYYY-MM-DD

            if home and away and fecha_iso:
                # Clave primaria: fecha + equipos (cruce exacto)
                xg_map[(fecha_iso, home, away)] = (xg_h, xg_a)
                # Clave secundaria: solo equipos (fallback si fecha no coincide exactamente)
                xg_map.setdefault(('', home, away), (xg_h, xg_a))
                total += 1

    print(f"   ✅ Understat ({temporada_actual} + {temporada_actual-1}): {total} partidos con xG cargados.")
    return xg_map


# ===========================================================================
# NORMALIZACIÓN DE NOMBRES
# Football-Data.org y Understat usan nombres distintos para el mismo equipo.
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
    "levante ud"                    : "levante",        # sin Understat
    "real oviedo"                   : "oviedo",         # sin Understat
    # Aliases temporadas anteriores
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
# ENTRENAMIENTO PRINCIPAL
# ===========================================================================

def train_spain():
    if not API_KEY:
        print("❌ ERROR: No se encontró FOOTBALL_DATA_API_KEY.")
        return

    try:
        print(f"Consultando LaLiga | λ={TIME_DECAY_LAMBDA} | xG weight={XG_WEIGHT}...")
        print("Descargando xG desde Understat (2 temporadas)...")
        xg_understat = obtener_xg_understat()

        response = requests.get(URL, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            print(f"❌ Error Football-Data ({response.status_code}): {response.text}")
            return

        matches = response.json().get('matches', [])
        if not matches:
            print("⚠️ No hay partidos terminados.")
            return

        goles = []
        team_ids = {}
        partidos_con_xg = 0
        partidos_sin_xg = 0
        partidos_sin_understat = 0
        equipos_sin_xg = set()

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

            # Equipos conocidos sin cobertura en Understat → solo goles reales, sin log de debug
            if home_us in EQUIPOS_SIN_UNDERSTAT or away_us in EQUIPOS_SIN_UNDERSTAT:
                valor_h = float(goals_h)
                valor_a = float(goals_a)
                partidos_sin_understat += 1
            else:
                # Cruce primario: fecha + equipos
                fecha_iso = m['utcDate'][:10]
                xg_data = xg_understat.get((fecha_iso, home_us, away_us))

                # Fallback: solo equipos (sin fecha), útil si hay desfase de 1 día UTC
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
                'home': home_name,
                'away': away_name,
                'goals_h': valor_h,
                'goals_a': valor_a,
                'date': m['utcDate']
            })

        if equipos_sin_xg:
            print(f"   🔍 Partidos sin xG (revisar mapeo):")
            for e in sorted(equipos_sin_xg):
                print(f"      - {e}")

        df = pd.DataFrame(goles)
        df['date'] = pd.to_datetime(df['date'])
        max_date = df['date'].max()
        df['days_since'] = (max_date - df['date']).dt.days
        df['weight'] = np.exp(-TIME_DECAY_LAMBDA * df['days_since'])

        avg_h = np.average(df['goals_h'], weights=df['weight'])
        avg_a = np.average(df['goals_a'], weights=df['weight'])

        teams_stats = {}
        for team in pd.unique(df[['home', 'away']].values.ravel()):
            h_df = df[df['home'] == team]
            a_df = df[df['away'] == team]

            att_h = np.average(h_df['goals_h'], weights=h_df['weight']) / avg_h if not h_df.empty else 1.0
            def_h = np.average(h_df['goals_a'], weights=h_df['weight']) / avg_a if not h_df.empty else 1.0
            att_a = np.average(a_df['goals_a'], weights=a_df['weight']) / avg_a if not a_df.empty else 1.0
            def_a = np.average(a_df['goals_h'], weights=a_df['weight']) / avg_h if not a_df.empty else 1.0

            teams_stats[team] = {
                "id_api": int(team_ids.get(team, 0)),
                "att_h": float(att_h),
                "def_h": float(def_h),
                "att_a": float(att_a),
                "def_a": float(def_a)
            }

        output = {
            "LaLiga": {
                "averages": {"league_home": float(avg_h), "league_away": float(avg_a)},
                "teams": teams_stats
            },
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "meta": {
                "partidos_con_xg": partidos_con_xg,
                "partidos_sin_xg": partidos_sin_xg,
                "partidos_sin_understat": partidos_sin_understat,
                "xg_weight": XG_WEIGHT,
                "time_decay_lambda": TIME_DECAY_LAMBDA
            }
        }

        with open('modelo_poisson.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=4, ensure_ascii=False)

        print(f"✅ modelo_poisson.json actualizado.")
        print(f"   Equipos: {len(teams_stats)}")
        print(f"   Con xG:          {partidos_con_xg}")
        print(f"   Sin xG (mapeo):  {partidos_sin_xg}")
        print(f"   Sin Understat:   {partidos_sin_understat} (Levante/Oviedo — normal)")

    except Exception as e:
        print(f"❌ Error crítico: {e}")


if __name__ == "__main__":
    train_spain()
