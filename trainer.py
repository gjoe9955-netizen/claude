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

API_KEY         = os.getenv("FOOTBALL_DATA_API_KEY")

BASE_URL = "https://api.football-data.org/v4/competitions/PD/matches?status=FINISHED"
HEADERS  = {"X-Auth-Token": API_KEY}
TIME_DECAY_LAMBDA = 0.007
XG_WEIGHT         = 0.6

EQUIPOS_SIN_UNDERSTAT = {"levante", "oviedo", "valladolid"}



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
        h_us    = normalizar_nombre(h_name)
        a_us    = normalizar_nombre(a_name)
        xg_dat  = xg_understat.get((fecha, h_us, a_us)) or xg_understat.get(('', h_us, a_us))
        xg_h    = round(xg_dat[0], 2) if xg_dat else None
        xg_a    = round(xg_dat[1], 2) if xg_dat else None
        pares[(h_id, a_id)].append({
            "fecha": fecha, "goles_h": goles_h, "goles_a": goles_a,
            "xg_h": xg_h, "xg_a": xg_a, "winner": winner
        })
    h2h_out = {}
    for (h_id, a_id), partidos in pares.items():
        partidos_ord      = sorted(partidos, key=lambda p: p["fecha"], reverse=True)[:6]
        temporadas_con_xg = sum(1 for p in partidos_ord if p["xg_h"] is not None)
        h2h_out[f"{h_id}_{a_id}"] = {"partidos": partidos_ord, "temporadas_con_xg": temporadas_con_xg}
    return h2h_out


def train_spain():
    if not API_KEY:
        print("❌ ERROR: No se encontró FOOTBALL_DATA_API_KEY.")
        return

    try:
        print(f"Consultando LaLiga | λ={TIME_DECAY_LAMBDA} | xG weight={XG_WEIGHT}...")
        print("Descargando xG desde Understat (2 temporadas)...")
        xg_understat = obtener_xg_understat()

        print("Descargando partidos desde Football-Data (2 temporadas)...")
        matches = descargar_partidos_football_data()
        if not matches:
            print("⚠️ No hay partidos terminados.")
            return

        goles = []
        team_ids = {}
        partidos_con_xg = partidos_sin_xg = partidos_sin_understat = 0
        equipos_sin_xg  = set()

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
                xg_data   = xg_understat.get((fecha_iso, home_us, away_us)) or xg_understat.get(('', home_us, away_us))
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

            goles.append({'home': home_name, 'away': away_name, 'goals_h': valor_h, 'goals_a': valor_a, 'date': m['utcDate']})

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
                "att_h": float(att_h), "def_h": float(def_h),
                "att_a": float(att_a), "def_a": float(def_a)
            }

        # Tarjetas: se preservan del modelo anterior (actualizadas por cards.py)
        tarjetas_data = {}
        if os.path.exists('modelo_poisson.json'):
            try:
                with open('modelo_poisson.json', 'r', encoding='utf-8') as f_prev:
                    modelo_prev = json.load(f_prev)
                liga_prev = next(iter(modelo_prev))
                for nombre_fd, stats_prev in modelo_prev[liga_prev].get('teams', {}).items():
                    if 'tarjetas' in stats_prev:
                        tarjetas_data[nombre_fd] = stats_prev['tarjetas']
                print(f"   ✅ Tarjetas preservadas: {len(tarjetas_data)} equipos del modelo anterior.")
            except Exception as e:
                print(f"   ⚠️  No se pudieron preservar tarjetas: {e}")

        for team in teams_stats:
            teams_stats[team]["tarjetas"] = tarjetas_data.get(team) or {
                "avg_amarillas": 2.1, "avg_rojas": 0.1, "partidos_analizados": 0
            }

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
                "partidos_totales":       len(goles),
                "partidos_con_xg":        partidos_con_xg,
                "partidos_sin_xg":        partidos_sin_xg,
                "partidos_sin_understat": partidos_sin_understat,
                "xg_weight":              XG_WEIGHT,
                "time_decay_lambda":      TIME_DECAY_LAMBDA,
                "pares_h2h":              len(h2h_data),
                "tarjetas_con_data":      sum(1 for t in teams_stats.values() if t["tarjetas"]["partidos_analizados"] > 0)
            }
        }

        with open('modelo_poisson.json', 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=4, ensure_ascii=False)

        print(f"\n✅ modelo_poisson.json actualizado (v4.3).")
        print(f"   Equipos:               {len(teams_stats)}")
        print(f"   Partidos totales:      {len(goles)}")
        print(f"   Con xG:                {partidos_con_xg}")
        print(f"   Sin xG (mapeo):        {partidos_sin_xg}")
        print(f"   Sin Understat:         {partidos_sin_understat} (Levante/Oviedo/Valladolid — normal)")
        print(f"   Pares H2H:             {len(h2h_data)}")
        print(f"   Equipos con tarjetas:  {output['meta']['tarjetas_con_data']} (preservadas de cards.py)")

    except Exception as e:
        print(f"❌ Error crítico: {e}")
        raise


if __name__ == "__main__":
    train_spain()
