import os
import requests
import json
import pandas as pd
import numpy as np
from datetime import datetime

# ===========================================================================
# Fuentes de datos:
#   - Football-Data.org → estructura de partidos, IDs de equipos, resultados
#   - Understat.com     → xG gratis via endpoint AJAX interno (sin API key)
# ===========================================================================

API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
URL = "https://api.football-data.org/v4/competitions/PD/matches?status=FINISHED"
HEADERS = {"X-Auth-Token": API_KEY}

TIME_DECAY_LAMBDA = 0.007
XG_WEIGHT = 0.6


# ===========================================================================
# UNDERSTAT — ENDPOINT AJAX INTERNO
# ===========================================================================

def obtener_xg_understat():
    """
    Obtiene xG de LaLiga desde el endpoint AJAX interno de Understat.
    Devuelve dict: {(home_norm, away_norm): (xg_h, xg_a)}
    """
    xg_map = {}

    anio = datetime.now().year
    mes = datetime.now().month
    temporada = anio - 1 if mes < 8 else anio

    headers_us = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://understat.com/league/La_liga",
        "Origin": "https://understat.com",
    }

    try:
        r = requests.post(
            "https://understat.com/main/getMatchesStats/",
            headers=headers_us,
            data=f"league=La_liga&season={temporada}",
            timeout=20
        )

        if r.status_code != 200:
            print(f"   ⚠️  Understat AJAX {r.status_code}. Solo goles reales.")
            return xg_map

        resp = r.json()
        if not resp.get('response'):
            print(f"   ⚠️  Understat AJAX response=false: {resp.get('error', '')}")
            return xg_map

        raw = resp.get('data', '[]')

        if isinstance(raw, str):
            raw = raw.replace("\\'", "'")
            try:
                raw = raw.encode('utf-8').decode('unicode_escape').encode('latin-1').decode('utf-8')
            except Exception:
                pass
            partidos = json.loads(raw)
        else:
            partidos = raw

        for p in partidos:
            if not p.get('isResult'):
                continue
            xg_data = p.get('xG', {})
            if not xg_data:
                continue
            try:
                xg_h = float(xg_data.get('h', 0))
                xg_a = float(xg_data.get('a', 0))
            except (TypeError, ValueError):
                continue
            if not (0 <= xg_h <= 8 and 0 <= xg_a <= 8):
                continue
            home = p.get('h', {}).get('title', '').strip().lower()
            away = p.get('a', {}).get('title', '').strip().lower()
            if home and away:
                xg_map[(home, away)] = (xg_h, xg_a)

        print(f"   ✅ Understat AJAX ({temporada}): {len(xg_map)} partidos con xG.")

    except Exception as e:
        print(f"   ⚠️  Error Understat AJAX: {e}. Solo goles reales.")

    return xg_map


# ===========================================================================
# NORMALIZACIÓN DE NOMBRES
# Football-Data.org y Understat usan nombres distintos para el mismo equipo.
# ===========================================================================

NOMBRE_FD_A_US = {
    "real madrid cf"                : "real madrid",
    "fc barcelona"                  : "barcelona",
    "atletico de madrid"            : "atletico madrid",
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
    "ud girona fc"                  : "girona",
    "deportivo alavés"              : "alaves",
    "deportivo alaves"              : "alaves",
    "ca osasuna"                    : "osasuna",
    "rc celta de vigo"              : "celta vigo",
    "ud almería"                    : "almeria",
    "ud almeria"                    : "almeria",
    "rcd mallorca"                  : "mallorca",
    "cd leganés"                    : "leganes",
    "cd leganes"                    : "leganes",
    "real valladolid cf"            : "valladolid",
    "cd espanyol de barcelona"      : "espanyol",
    "ud las palmas"                 : "las palmas",
    "real racing club de santander" : "racing santander",
}

def normalizar_nombre(nombre_fd):
    key = nombre_fd.strip().lower()
    return NOMBRE_FD_A_US.get(key, key)


# ===========================================================================
# ENTRENAMIENTO PRINCIPAL
# ===========================================================================

def train_spain():
    if not API_KEY:
        print("❌ ERROR: No se encontró la API KEY.")
        return

    try:
        print(f"Consultando LaLiga | λ={TIME_DECAY_LAMBDA} | xG weight={XG_WEIGHT}...")

        print("Descargando xG desde Understat AJAX...")
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
            xg_data = xg_understat.get((home_us, away_us))

            if xg_data:
                xg_h, xg_a = xg_data
                valor_h = (xg_h * XG_WEIGHT) + (goals_h * (1 - XG_WEIGHT))
                valor_a = (xg_a * XG_WEIGHT) + (goals_a * (1 - XG_WEIGHT))
                partidos_con_xg += 1
            else:
                valor_h = float(goals_h)
                valor_a = float(goals_a)
                partidos_sin_xg += 1

            goles.append({
                'home': home_name,
                'away': away_name,
                'goals_h': valor_h,
                'goals_a': valor_a,
                'date': m['utcDate']
            })

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
                "xg_weight": XG_WEIGHT,
                "time_decay_lambda": TIME_DECAY_LAMBDA
            }
        }

        with open('modelo_poisson.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=4, ensure_ascii=False)

        print(f"✅ modelo_poisson.json actualizado.")
        print(f"   Equipos: {len(teams_stats)} | Con xG: {partidos_con_xg} | Sin xG: {partidos_sin_xg}")

    except Exception as e:
        print(f"❌ Error crítico: {e}")

if __name__ == "__main__":
    train_spain()
