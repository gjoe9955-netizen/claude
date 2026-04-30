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
# MEJORA 3: Time-decay aumentado de 0.005 a 0.007
# Con 0.005 un partido de hace 6 meses pesaba ~17%.
# Con 0.007 pesa ~12%, dando más importancia a las últimas 8 jornadas.
# ===========================================================================
TIME_DECAY_LAMBDA = 0.007

def train_spain():
    if not API_KEY:
        print("❌ ERROR: No se encontró la API KEY. Verifica tus Secrets en GitHub.")
        return

    try:
        print(f"Consultando LaLiga Española, aplicando Time-Decay λ={TIME_DECAY_LAMBDA}...")
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

        for m in matches:
            if m.get('score') and m['score'].get('fullTime'):
                home_name = m['homeTeam']['name']
                away_name = m['awayTeam']['name']

                team_ids[home_name] = m['homeTeam']['id']
                team_ids[away_name] = m['awayTeam']['id']

                goles.append({
                    'home': home_name,
                    'away': away_name,
                    'goals_h': m['score']['fullTime']['home'],
                    'goals_a': m['score']['fullTime']['away'],
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

        output = {
            "LaLiga": {
                "averages": {"league_home": float(avg_h), "league_away": float(avg_a)},
                "teams": teams_stats
            },
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "time_decay_lambda": TIME_DECAY_LAMBDA
        }

        with open('modelo_poisson.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=4, ensure_ascii=False)

        print(f"✅ modelo_poisson.json actualizado. Equipos: {len(teams_stats)} | λ={TIME_DECAY_LAMBDA}")

    except Exception as e:
        print(f"❌ Error crítico: {e}")

if __name__ == "__main__":
    train_spain()
