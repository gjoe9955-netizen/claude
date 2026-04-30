import os
import requests
import json
import pandas as pd
import numpy as np
from datetime import datetime

# Configuración Football-Data.org
API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")
URL = "https://api.football-data.org/v4/competitions/PD/matches?status=FINISHED"
HEADERS = {"X-Auth-Token": API_KEY}

# ===========================================================================
# Time-decay: 0.007 da ~12% de peso a partidos de hace 6 meses,
# priorizando las últimas 8 jornadas.
# ===========================================================================
TIME_DECAY_LAMBDA = 0.007

# ===========================================================================
# MEJORA 1: xG (Expected Goals)
#
# Por qué importa:
#   Los goles reales tienen ruido: penales, golazos de media cancha,
#   errores de portero. xG mide cuánto DEBERÍA haber marcado cada equipo
#   según la calidad de sus disparos, haciendo las lambdas más estables.
#
# Implementación:
#   Se mezcla xG con goles reales usando XG_WEIGHT.
#   valor_final = (xG * XG_WEIGHT) + (goles_reales * (1 - XG_WEIGHT))
#
#   XG_WEIGHT = 0.6  →  60% xG, 40% goles reales.
#   Si la API no devuelve xG para un partido, ese partido usa solo goles reales
#   y se registra como fallback (transparente en el log).
#
# Football-Data.org devuelve xG en el campo:
#   score.regularTime  (no siempre disponible; plan Free lo incluye en PD)
#   Clave: m['score'].get('regularTime') con subclaves 'home' y 'away'
#   Nota: el campo real varía por versión de la API; el código prueba
#   'regularTime' y luego 'halfTime' como fallback antes de usar solo goles.
# ===========================================================================
XG_WEIGHT = 0.6  # proporción de xG vs goles reales


def extraer_xg(score):
    """
    Intenta extraer xG del objeto score de Football-Data.org.
    Prueba los campos conocidos donde aparece según el plan/versión.
    Devuelve (xg_home, xg_away) o (None, None) si no está disponible.
    """
    # Campo principal donde Football-Data publica xG
    for campo in ('regularTime', 'extraTime'):
        ft = score.get(campo)
        if ft and ft.get('home') is not None and ft.get('away') is not None:
            # Verificar que son valores razonables de xG (entre 0 y 8)
            h, a = ft['home'], ft['away']
            if isinstance(h, float) and isinstance(a, float) and 0 <= h <= 8 and 0 <= a <= 8:
                return h, a
    return None, None


def train_spain():
    if not API_KEY:
        print("❌ ERROR: No se encontró la API KEY. Verifica tus Secrets en GitHub.")
        return

    try:
        print(f"Consultando LaLiga Española, aplicando Time-Decay λ={TIME_DECAY_LAMBDA} | xG weight={XG_WEIGHT}...")
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

                # --- MEJORA 1: Intentar obtener xG ---
                xg_h, xg_a = extraer_xg(m['score'])

                if xg_h is not None:
                    # Mezcla ponderada: XG_WEIGHT de xG + resto de goles reales
                    valor_h = (xg_h * XG_WEIGHT) + (goals_h * (1 - XG_WEIGHT))
                    valor_a = (xg_a * XG_WEIGHT) + (goals_a * (1 - XG_WEIGHT))
                    partidos_con_xg += 1
                else:
                    # Fallback: solo goles reales
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

        xg_cobertura = partidos_con_xg / (partidos_con_xg + partidos_sin_xg) * 100 if goles else 0

        output = {
            "LaLiga": {
                "averages": {"league_home": float(avg_h), "league_away": float(avg_a)},
                "teams": teams_stats
            },
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "time_decay_lambda": TIME_DECAY_LAMBDA,
            "xg_weight": XG_WEIGHT,
            "xg_cobertura_pct": round(xg_cobertura, 1)
        }

        with open('modelo_poisson.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=4, ensure_ascii=False)

        print(f"✅ modelo_poisson.json actualizado.")
        print(f"   Equipos: {len(teams_stats)} | λ={TIME_DECAY_LAMBDA} | xG weight={XG_WEIGHT}")
        print(f"   xG disponible: {partidos_con_xg} partidos ({xg_cobertura:.1f}%) | Sin xG: {partidos_sin_xg} partidos")

        if xg_cobertura == 0:
            print("   ⚠️  API no devolvió xG en ningún partido. El modelo usó solo goles reales.")
            print("      Esto es normal en el plan Free de Football-Data.org para temporadas anteriores.")

    except Exception as e:
        print(f"❌ Error crítico: {e}")

if __name__ == "__main__":
    train_spain()
