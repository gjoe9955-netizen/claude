import os
import requests
import json
import pandas as pd
import numpy as np
from datetime import datetime

# ===========================================================================
# Fuentes de datos:
#   - Football-Data.org → estructura de partidos, IDs de equipos, resultados
#   - Understat.com     → xG gratis, sin API key (scraping del JSON embebido)
# ===========================================================================

# Configuración Football-Data.org
API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
URL = "https://api.football-data.org/v4/competitions/PD/matches?status=FINISHED"
HEADERS = {"X-Auth-Token": API_KEY}

# Understat — URL de LaLiga (temporada actual; se actualiza automáticamente)
UNDERSTAT_URL = "https://understat.com/league/La_liga"

# ===========================================================================
# Time-decay: 0.007 da ~12% de peso a partidos de hace 6 meses,
# priorizando las últimas 8 jornadas.
# ===========================================================================
TIME_DECAY_LAMBDA = 0.007

# ===========================================================================
# xG Weight: mezcla xG con goles reales.
#   valor_final = (xG * XG_WEIGHT) + (goles_reales * (1 - XG_WEIGHT))
#   XG_WEIGHT = 0.6  →  60% xG, 40% goles reales.
# ===========================================================================
XG_WEIGHT = 0.6


# ===========================================================================
# UNDERSTAT SCRAPER
#
# Understat embebe todos los datos del partido en el HTML de la página
# como una variable JS: datesData = JSON.parse('...')
# No requiere API key. Se parsea con regex y json.loads.
#
# Formato de cada partido en datesData:
#   {
#     "id": "...",
#     "h": {"title": "Real Madrid", ...},
#     "a": {"title": "Barcelona", ...},
#     "goals": {"h": "2", "a": "1"},
#     "xG": {"h": "1.82", "a": "1.23"},
#     "datetime": "2024-09-14 20:00:00",
#     "isResult": true
#   }
# ===========================================================================

def obtener_xg_understat():
    """
    Descarga la página de Understat de LaLiga y extrae el JSON embebido
    con xG de todos los partidos de la temporada.

    Devuelve dict: {(home_norm, away_norm): (xg_h, xg_a)}
    Donde home_norm y away_norm son nombres en minúsculas sin espacios extra.
    """
    import re

    xg_map = {}
    try:
        headers_us = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(UNDERSTAT_URL, headers=headers_us, timeout=20)
        if r.status_code != 200:
            print(f"   ⚠️  Understat respondió {r.status_code}. Se usará solo goles reales.")
            return xg_map

        # Understat embebe los datos como: var datesData = JSON.parse('...')
        # Probar varias variantes del patrón por si cambia el formato
        PATRONES = [
            r"var\s+datesData\s*=\s*JSON\.parse\('(.+?)'\)",
            r"datesData\s*=\s*JSON\.parse\('(.+?)'\)",
            r"datesData\s*=\s*JSON\.parse\(\"(.+?)\"\)",
            r"'datesData'\s*,\s*JSON\.parse\('(.+?)'\)",
        ]

        match = None
        for patron in PATRONES:
            match = re.search(patron, r.text)
            if match:
                break

        if not match:
            vars_encontradas = re.findall(r"var\s+(\w+Data)\s*=", r.text)
            print(f"   ⚠️  No se encontró datesData en Understat.")
            if vars_encontradas:
                print(f"      Variables *Data en HTML: {vars_encontradas}")
                print(f"      → Actualiza el regex con el nombre correcto.")
            else:
                print(f"      → Página vacía o bloqueada. HTTP {r.status_code}, {len(r.text)} bytes.")
            return xg_map

        # El contenido está escapado doble: primero decodificar el escape de JS
        raw = match.group(1)
        raw = raw.replace("\\'", "'")
        # Decodificar unicode escapes (\uXXXX)
        raw = raw.encode('utf-8').decode('unicode_escape').encode('latin-1').decode('utf-8')

        partidos = json.loads(raw)

        for p in partidos:
            # Solo partidos con resultado
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

            # Validación básica
            if not (0 <= xg_h <= 8 and 0 <= xg_a <= 8):
                continue

            home_name = p.get('h', {}).get('title', '').strip().lower()
            away_name = p.get('a', {}).get('title', '').strip().lower()

            if home_name and away_name:
                xg_map[(home_name, away_name)] = (xg_h, xg_a)

        print(f"   ✅ Understat: {len(xg_map)} partidos con xG cargados.")

    except Exception as e:
        print(f"   ⚠️  Error scraping Understat: {e}. Se usará solo goles reales.")

    return xg_map


# ===========================================================================
# NORMALIZACIÓN DE NOMBRES
#
# Football-Data.org y Understat usan nombres distintos para el mismo equipo.
# Esta tabla cubre los casos comunes de LaLiga.
# Si un equipo nuevo no aparece, el fallback es goles reales (sin xG).
# ===========================================================================

NOMBRE_FD_A_US = {
    # Football-Data name                : Understat name (lowercase)
    "real madrid cf"                    : "real madrid",
    "fc barcelona"                      : "barcelona",
    "atletico de madrid"                : "atletico madrid",
    "athletic club"                     : "athletic club",
    "real sociedad de fútbol"           : "real sociedad",
    "real sociedad de futbol"           : "real sociedad",
    "villarreal cf"                     : "villarreal",
    "real betis balompié"               : "real betis",
    "real betis balompie"               : "real betis",
    "sevilla fc"                        : "sevilla",
    "valencia cf"                       : "valencia",
    "rayo vallecano de madrid"          : "rayo vallecano",
    "getafe cf"                         : "getafe",
    "ud girona fc"                      : "girona",
    "deportivo alavés"                  : "alaves",
    "deportivo alaves"                  : "alaves",
    "ca osasuna"                        : "osasuna",
    "rc celta de vigo"                  : "celta vigo",
    "ud almería"                        : "almeria",
    "ud almeria"                        : "almeria",
    "rcd mallorca"                      : "mallorca",
    "cd leganés"                        : "leganes",
    "cd leganes"                        : "leganes",
    "real valladolid cf"                : "valladolid",
    "cd espanyol de barcelona"          : "espanyol",
    "ud las palmas"                     : "las palmas",
    "real racing club de santander"     : "racing santander",
}

def normalizar_nombre(nombre_fd):
    """
    Convierte nombre de Football-Data.org al equivalente en Understat.
    Si no está en el mapa, devuelve el nombre en minúsculas tal cual.
    """
    key = nombre_fd.strip().lower()
    return NOMBRE_FD_A_US.get(key, key)


# ===========================================================================
# ENTRENAMIENTO PRINCIPAL
# ===========================================================================

def train_spain():
    if not API_KEY:
        print("❌ ERROR: No se encontró la API KEY. Verifica tus Secrets en GitHub.")
        return

    try:
        print(f"Consultando LaLiga Española, aplicando Time-Decay λ={TIME_DECAY_LAMBDA} | xG weight={XG_WEIGHT}...")

        # --- Paso 1: Descargar xG de Understat ---
        print("Descargando xG desde Understat...")
        xg_understat = obtener_xg_understat()

        # --- Paso 2: Descargar partidos de Football-Data.org ---
        response = requests.get(URL, headers=HEADERS, timeout=15)

        if response.status_code != 200:
            print(f"❌ Error API ({response.status_code}): {response.text}")
            return

        data = response.json()
        matches = data.get('matches', [])

        if not matches:
            print("⚠️ No hay partidos terminados disponibles.")
            return

        goles = []
        team_ids = {}
        partidos_con_xg = 0
        partidos_sin_xg = 0

        for m in matches:
            if m.get('score') and m['score'].get('fullTime'):
                home_name = m['homeTeam']['name']
                away_name = m['awayTeam']['name']

                team_ids[home_name] = m['homeTeam']['id']
                team_ids[away_name] = m['awayTeam']['id']

                goals_h = m['score']['fullTime']['home']
                goals_a = m['score']['fullTime']['away']

                # --- Intentar obtener xG de Understat ---
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
        teams = pd.unique(df[['home', 'away']].values.ravel())

        for team in teams:
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

        total_partidos = partidos_con_xg + partidos_sin_xg
        xg_cobertura = partidos_con_xg / total_partidos * 100 if total_partidos else 0

        output = {
            "LaLiga": {
                "averages": {"league_home": float(avg_h), "league_away": float(avg_a)},
                "teams": teams_stats
            },
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "time_decay_lambda": TIME_DECAY_LAMBDA,
            "xg_weight": XG_WEIGHT,
            "xg_cobertura_pct": round(xg_cobertura, 1),
            "xg_source": "understat.com"
        }

        with open('modelo_poisson.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=4, ensure_ascii=False)

        print(f"✅ modelo_poisson.json actualizado.")
        print(f"   Equipos: {len(teams_stats)} | λ={TIME_DECAY_LAMBDA} | xG weight={XG_WEIGHT}")
        print(f"   xG (Understat): {partidos_con_xg} partidos ({xg_cobertura:.1f}%) | Sin xG: {partidos_sin_xg} partidos")

        if xg_cobertura == 0:
            print("   ⚠️  No se pudo obtener xG de Understat. El modelo usó solo goles reales.")
            print("      Posibles causas: bloqueo de IP en CI/CD, cambio de formato HTML, temporada no iniciada.")
        elif xg_cobertura < 50:
            print(f"   ⚠️  Cobertura xG baja ({xg_cobertura:.1f}%). Revisar tabla NOMBRE_FD_A_US en trainer.py.")
            print("      Equipos sin xG probablemente tienen nombre distinto entre Football-Data y Understat.")

    except Exception as e:
        print(f"❌ Error crítico: {e}")


if __name__ == "__main__":
    train_spain()
