import os
import json
import asyncio
import logging
import requests
import base64
import html 
from scipy.stats import poisson
from datetime import datetime, timedelta, timezone

import telebot
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# --- Configuración de Entorno ---
logging.basicConfig(level=logging.INFO)
load_dotenv()

TOKEN = os.getenv('TOKEN_TELEGRAM')
GROQ_KEY = os.getenv('GROQ_API_KEY')
SAMBA_KEY = os.getenv('SAMBA_KEY')
FOOTBALL_DATA_KEY = os.getenv('FOOTBALL_DATA_API_KEY')
ODDS_API_KEY = os.getenv('API_KEY_ODDS')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
SERPER_KEY = os.getenv('SERPER_API_KEY') 

OFFSET_JUAREZ = -6
URL_JSON = "https://raw.githubusercontent.com/gjoe9955-netizen/claude/main/modelo_poisson.json"
REPO_OWNER = "gjoe9955-netizen"
REPO_NAME = "claude"
FILE_PATH = "historial.json"

bot = AsyncTeleBot(TOKEN)
COOLDOWN = {}
COOLDOWN_MINUTOS = 30

# --- Cache del modelo en memoria ---
_MODELO_CACHE = {"data": None, "ts": None}
CACHE_TTL_SEGUNDOS = 3600

async def obtener_modelo():
    """Carga modelo_poisson.json desde GitHub, con cache de 1 hora."""
    ahora = datetime.now(timezone.utc)
    if _MODELO_CACHE["data"] and _MODELO_CACHE["ts"] and (ahora - _MODELO_CACHE["ts"]).total_seconds() < CACHE_TTL_SEGUNDOS:
        return _MODELO_CACHE["data"], True
    try:
        r = await asyncio.to_thread(requests.get, URL_JSON, timeout=10)
        if r.status_code == 200:
            _MODELO_CACHE["data"] = r.json()
            _MODELO_CACHE["ts"] = ahora
            return _MODELO_CACHE["data"], True
    except:
        pass
    return _MODELO_CACHE["data"], False

# --- Función de Búsqueda de Última Hora (Serper) ---
PALABRAS_BAJA_LOCAL = ["baja", "lesión", "lesionado", "no jugará", "ausente", "descartado", "out"]
PALABRAS_BAJA_VISITA = PALABRAS_BAJA_LOCAL

async def obtener_contexto_real(l_q, v_q):
    """
    Devuelve (texto_noticias, factor_penalty_local, factor_penalty_visita).
    """
    if not SERPER_KEY:
        return "No hay API Key de Serper configurada.", 1.0, 1.0
    
    url = "https://google.serper.dev/search"
    query = f'(site:jornadaperfecta.com OR site:futbolfantasy.com) "{l_q}" "{v_q}" alineación'
    
    payload = json.dumps({
        "q": query,
        "gl": "es",
        "hl": "es",
        "tbs": "qdr:w" 
    })
    headers = {
        'X-API-KEY': SERPER_KEY,
        'Content-Type': 'application/json'
    }
    
    try:
        r = await asyncio.to_thread(requests.post, url, headers=headers, data=payload, timeout=10)
        res = r.json().get('organic', [])
        contexto = ""
        penalty_local = 1.0
        penalty_visita = 1.0

        for item in res[:3]:
            snippet = item.get('snippet', '').lower()
            titulo = item.get('title', '').lower()
            texto_completo = snippet + " " + titulo
            contexto += f"- {item['title']}: {item['snippet']}\n"

            if l_q.lower() in texto_completo:
                if any(p in texto_completo for p in PALABRAS_BAJA_LOCAL):
                    penalty_local = 0.95

            if v_q.lower() in texto_completo:
                if any(p in texto_completo for p in PALABRAS_BAJA_VISITA):
                    penalty_visita = 0.95

        contexto_final = contexto if contexto else "No se encontraron noticias recientes."
        return contexto_final, penalty_local, penalty_visita

    except Exception as e:
        logging.error(f"Error Serper: {e}")
        return "Error consultando noticias de última hora.", 1.0, 1.0

# --- Persistencia en GitHub ---
async def guardar_en_github(nuevo_registro=None, historial_completo=None):
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        # GET para obtener SHA actual del archivo
        r_get = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
        r_get_json = r_get.json()

        if r_get.status_code == 200:
            sha = r_get_json.get('sha')
            contenido_raw = r_get_json.get('content', '')
            # La API de GitHub devuelve el contenido con saltos de línea embebidos
            historial_actual = json.loads(base64.b64decode(contenido_raw.replace('\n', '')).decode('utf-8'))
        elif r_get.status_code == 404:
            sha = None          # Archivo nuevo: no se necesita SHA
            historial_actual = []
        else:
            logging.error(f"GitHub GET falló: {r_get.status_code} — {r_get_json}")
            return

        if historial_completo is None:
            if nuevo_registro:
                historial_actual.append(nuevo_registro)
            historial = historial_actual
        else:
            historial = historial_completo

        nuevo_contenido = base64.b64encode(
            json.dumps(historial, indent=4, ensure_ascii=False).encode('utf-8')
        ).decode('utf-8')

        payload = {
            "message": "🤖 Actualización de Historial",
            "content": nuevo_contenido,
        }
        # SHA es obligatorio si el archivo ya existe; omitirlo si es nuevo
        if sha:
            payload["sha"] = sha

        r_put = await asyncio.to_thread(requests.put, url, headers=headers, json=payload, timeout=15)
        if r_put.status_code not in (200, 201):
            logging.error(f"GitHub PUT falló: {r_put.status_code} — {r_put.json()}")
        else:
            logging.info(f"✅ historial.json actualizado en GitHub ({len(historial)} registros)")

    except Exception as e:
        logging.error(f"Error GitHub: {e}", exc_info=True)

# --- Estado Global ---
SISTEMA_IA = {
    "estratega": {"api": None, "nodo": None},
    "auditor": {"api": None, "nodo": None},

    "nodos_samba": [
        "DeepSeek-V3.2 [EST] | 99%",
        "DeepSeek-V3.1 [EST] | 95%",
        "Meta-Llama-3.3-70B [AUD] | 99%",
        "gemma-3-12b-it [EST] | 92%"
    ],

    "nodos_groq": [
        "llama-3.3-70b-versatile [EST] | 99%",
        "qwen/qwen3-32b [EST] | 90%",
        "meta-llama/llama-4-scout-17b-16e-instruct [AUD] | 98%",
        "openai/gpt-oss-20b [AUD] | 94%"
    ]
}

# --- Motores de IA (Groq & SambaNova) ---
async def ejecutar_ia(rol, prompt):
    config = SISTEMA_IA[rol]
    if not config["nodo"]: return None
    
    nodo_real = config["nodo"].split(" [")[0]
    
    if config["api"] == 'GROQ':
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    else:
        url = "https://api.sambanova.ai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {SAMBA_KEY}", "Content-Type": "application/json"}

    payload = {
        "model": nodo_real,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1
    }

    try:
        r = await asyncio.to_thread(requests.post, url, headers=headers, json=payload, timeout=15)
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        logging.error(f"Error IA {config['api']}: {e}")
        return f"❌ Error en Nodo {config['api']}"

# --- Núcleo Estadístico y APIs ---
async def obtener_datos_mercado(equipo_l):
    """
    Promedia cuotas de múltiples casas de apuestas (consenso de mercado).
    Prioriza casas con menor margen (más eficientes): Pinnacle primero, luego resto.
    Devuelve (cuota_local, cuota_empate, cuota_visita, check_odds).
    """
    if not ODDS_API_KEY: return 1.85, 3.50, 4.00, False

    # Casas preferidas por eficiencia de mercado (menor overround)
    CASAS_PREFERIDAS = {
        "pinnacle", "betfair", "bet365", "williamhill",
        "unibet", "bwin", "betway", "marathonbet"
    }
    MAX_CASAS = 6  # máximo de casas a promediar

    try:
        url = "https://api.the-odds-api.com/v4/sports/soccer_spain_la_liga/odds/"
        params = {'apiKey': ODDS_API_KEY, 'regions': 'eu', 'markets': 'h2h'}
        r = await asyncio.to_thread(requests.get, url, params=params, timeout=10)
        if r.status_code != 200:
            return 1.85, 3.50, 4.00, False

        for match in r.json():
            home = match['home_team'].lower()
            query = equipo_l.lower()
            if not (query in home or home in query):
                continue

            # Recopilar cuotas por casa, ordenando preferidas primero
            bookmakers = match.get('bookmakers', [])
            bookmakers_ordenados = sorted(
                bookmakers,
                key=lambda b: (0 if b['key'] in CASAS_PREFERIDAS else 1)
            )

            ol_list, oe_list, ov_list = [], [], []
            casas_usadas = []

            for bm in bookmakers_ordenados[:MAX_CASAS]:
                try:
                    outcomes = bm['markets'][0]['outcomes']
                    ol = next(o['price'] for o in outcomes if o['name'] == match['home_team'])
                    ov = next(o['price'] for o in outcomes if o['name'] == match['away_team'])
                    oe = next(o['price'] for o in outcomes if o['name'] == 'Draw')
                    ol_list.append(ol)
                    oe_list.append(oe)
                    ov_list.append(ov)
                    casas_usadas.append(bm['key'])
                except (StopIteration, KeyError, IndexError):
                    continue

            if not ol_list:
                return 1.85, 3.50, 4.00, False

            # Promedio simple de las casas disponibles
            ol_consenso = round(sum(ol_list) / len(ol_list), 3)
            oe_consenso = round(sum(oe_list) / len(oe_list), 3)
            ov_consenso = round(sum(ov_list) / len(ov_list), 3)

            logging.info(
                f"[Odds] Consenso de {len(casas_usadas)} casas: "
                f"L={ol_consenso} E={oe_consenso} V={ov_consenso} "
                f"| Casas: {casas_usadas}"
            )
            return ol_consenso, oe_consenso, ov_consenso, True

    except Exception as e:
        logging.error(f"Error obtener_datos_mercado: {e}")

    return 1.85, 3.50, 4.00, False

# --- Over/Under como señal de confirmación ---
async def obtener_confirmacion_ou(equipo_l, lambda_h, lambda_a):
    if not ODDS_API_KEY:
        return 1.0, "O/U: Sin API"
    try:
        url = "https://api.the-odds-api.com/v4/sports/soccer_spain_la_liga/odds/"
        params = {'apiKey': ODDS_API_KEY, 'regions': 'eu', 'markets': 'totals'}
        r = await asyncio.to_thread(requests.get, url, params=params, timeout=10)
        if r.status_code == 200:
            for match in r.json():
                home = match['home_team'].lower()
                query = equipo_l.lower()
                if query in home or home in query:
                    for bm in match['bookmakers']:
                        for mkt in bm['markets']:
                            if mkt['key'] == 'totals':
                                over_price = next((o['price'] for o in mkt['outcomes'] if o['name'] == 'Over'), None)
                                if over_price:
                                    prob_over_mercado = 1 / over_price
                                    prob_over_poisson = 0.0
                                    for x in range(7):
                                        for y in range(7):
                                            p = poisson.pmf(x, lambda_h) * poisson.pmf(y, lambda_a) * dixon_coles_tau(x, y, lambda_h, lambda_a)
                                            if x + y > 2:
                                                prob_over_poisson += p
                                    diff = prob_over_poisson - prob_over_mercado
                                    if diff > 0.05:
                                        return 1.2, f"O/U ✅ Confirmación ({prob_over_poisson*100:.0f}% vs {prob_over_mercado*100:.0f}%)"
                                    elif diff < -0.05:
                                        return 0.8, f"O/U ⚠️ Contradicción ({prob_over_poisson*100:.0f}% vs {prob_over_mercado*100:.0f}%)"
                                    else:
                                        return 1.0, f"O/U ➡️ Neutro ({prob_over_poisson*100:.0f}% vs {prob_over_mercado*100:.0f}%)"
    except Exception as e:
        logging.error(f"Error O/U: {e}")
    return 1.0, "O/U: Sin datos"

# --- Calibración del modelo con historial propio ---
async def obtener_factor_calibracion():
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}"
    try:
        r = await asyncio.to_thread(requests.get, url, timeout=10)
        if r.status_code != 200:
            return 1.0
        historial = r.json()
        completados = [h for h in historial if h.get('status') in ('✅ WIN', '❌ LOSS') and 'poisson' in h]
        if len(completados) < 10:
            return 1.0
        wins = sum(1 for h in completados if h['status'] == '✅ WIN')
        tasa_real = wins / len(completados)
        tasa_predicha = sum(float(h['poisson'].replace('%', '')) / 100 for h in completados) / len(completados)
        if tasa_predicha == 0:
            return 1.0
        factor = tasa_real / tasa_predicha
        return max(0.85, min(1.15, factor))
    except:
        return 1.0

async def api_football_call(endpoint):
    headers = {'X-Auth-Token': FOOTBALL_DATA_KEY}
    try:
        r = await asyncio.to_thread(requests.get, f"https://api.football-data.org/v4/competitions/PD/{endpoint}", headers=headers, timeout=10)
        return r.json() if r.status_code == 200 else None
    except: return None

# --- H2H filtrado por sede ---
async def obtener_h2h_directo(id_l, id_v):
    if not id_l or not id_v:
        return "H2H: Sin IDs válidos.", False, 0, 0, 0
    
    headers = {'X-Auth-Token': FOOTBALL_DATA_KEY}
    try:
        url = f"https://api.football-data.org/v4/teams/{id_l}/matches?competitors={id_v}&status=FINISHED"
        r = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
        if r.status_code == 200:
            matches = r.json().get('matches', [])
            if matches:
                l, v, e = 0, 0, 0
                for m in matches[:5]:
                    w = m['score']['winner']
                    home_id = m['homeTeam']['id']
                    if home_id == id_l:
                        if w == 'HOME_TEAM': l += 1
                        elif w == 'AWAY_TEAM': v += 1
                        else: e += 1
                    else:
                        if w == 'HOME_TEAM': v += 1
                        elif w == 'AWAY_TEAM': l += 1
                        else: e += 1
                total = l + v + e
                return f"Local {l} | Visitante {v} | Empates {e} (sede-ajustado)", True, l, v, total
        return "H2H: Sin datos directos.", False, 0, 0, 0
    except:
        return "H2H: Error API.", False, 0, 0, 0


def calcular_factor_h2h(home_wins, away_wins, total_partidos):
    if total_partidos < 3:
        return 1.0, 1.0, "H2H: Insuficientes datos"

    tasa_local = home_wins / total_partidos
    tasa_visita = away_wins / total_partidos
    MAX_AJUSTE = 0.08

    if tasa_local > 0.60:
        intensidad = min((tasa_local - 0.60) / 0.40, 1.0)
        ajuste = MAX_AJUSTE * intensidad
        factor_lh = 1.0 + ajuste
        factor_la = 1.0 - ajuste
        texto = f"H2H 🏠 Dominio local ({tasa_local*100:.0f}%, +{ajuste*100:.1f}% lh)"
    elif tasa_visita > 0.60:
        intensidad = min((tasa_visita - 0.60) / 0.40, 1.0)
        ajuste = MAX_AJUSTE * intensidad
        factor_lh = 1.0 - ajuste
        factor_la = 1.0 + ajuste
        texto = f"H2H 🚩 Dominio visita ({tasa_visita*100:.0f}%, +{ajuste*100:.1f}% la)"
    else:
        factor_lh = 1.0
        factor_la = 1.0
        texto = f"H2H ⚖️ Equilibrado ({home_wins}L/{away_wins}V)"

    return factor_lh, factor_la, texto


# --- Forma reciente (últimos 5 partidos) ---
async def obtener_forma_reciente(team_id):
    if not team_id:
        return 1.0, 1.0, "Forma: Sin ID"

    headers = {'X-Auth-Token': FOOTBALL_DATA_KEY}
    try:
        url = f"https://api.football-data.org/v4/teams/{team_id}/matches?status=FINISHED&limit=5"
        r = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
        if r.status_code != 200:
            return 1.0, 1.0, "Forma: Sin datos"

        matches = r.json().get('matches', [])
        if not matches:
            return 1.0, 1.0, "Forma: Sin partidos"

        puntos = 0
        for m in matches[:5]:
            w = m['score']['winner']
            home_id = m['homeTeam']['id']
            es_local = (home_id == team_id)
            if (es_local and w == 'HOME_TEAM') or (not es_local and w == 'AWAY_TEAM'):
                puntos += 3
            elif w == 'DRAW':
                puntos += 1

        MAX_AJUSTE = 0.10
        forma_norm = puntos / 15.0

        if forma_norm > 0.67:
            intensidad = (forma_norm - 0.67) / 0.33
            ajuste = MAX_AJUSTE * intensidad
            factor_ataque = 1.0 + ajuste
            factor_defensa = 1.0 - ajuste
            simbolo = "🔥"
        elif forma_norm < 0.33:
            intensidad = (0.33 - forma_norm) / 0.33
            ajuste = MAX_AJUSTE * intensidad
            factor_ataque = 1.0 - ajuste
            factor_defensa = 1.0 + ajuste
            simbolo = "❄️"
        else:
            factor_ataque = 1.0
            factor_defensa = 1.0
            simbolo = "➡️"

        texto = f"Forma {simbolo} {puntos}pts/15 (factor atk ×{factor_ataque:.3f})"
        return factor_ataque, factor_defensa, texto

    except Exception as e:
        logging.error(f"Error forma reciente team {team_id}: {e}")
        return 1.0, 1.0, "Forma: Error API"


# --- Posición en tabla ---
async def obtener_posiciones_tabla():
    try:
        data = await api_football_call("standings")
        if not data:
            return {}
        tabla = {}
        for t in data['standings'][0]['table']:
            tabla[t['team']['id']] = {
                'pos': t['position'],
                'puntos': t['points'],
                'nombre': t['team']['name']
            }
        return tabla
    except Exception as e:
        logging.error(f"Error tabla: {e}")
        return {}


def calcular_factor_tabla(pos_local, pos_visita, pts_local, pts_visita):
    MAX_AJUSTE = 0.06
    diff_pos = pos_visita - pos_local

    if abs(diff_pos) < 6:
        return 1.0, 1.0, f"Tabla ⚖️ Diferencia leve ({pos_local}° vs {pos_visita}°, {pts_local}pts vs {pts_visita}pts)"

    intensidad = min((abs(diff_pos) - 6) / 14, 1.0)
    ajuste = MAX_AJUSTE * intensidad

    if diff_pos > 0:
        factor_lh = 1.0 + ajuste
        factor_la = 1.0 - ajuste
        texto = f"Tabla 📈 Local superior ({pos_local}° vs {pos_visita}°, +{ajuste*100:.1f}% lh)"
    else:
        factor_lh = 1.0 - ajuste
        factor_la = 1.0 + ajuste
        texto = f"Tabla 📉 Visita superior ({pos_local}° vs {pos_visita}°, +{ajuste*100:.1f}% la)"

    return factor_lh, factor_la, texto


# --- Dixon-Coles: corrección para marcadores bajos ---
DC_RHO = -0.13

def dixon_coles_tau(x, y, lh, la, rho=DC_RHO):
    if x == 0 and y == 0:
        return 1.0 - (lh * la * rho)
    if x == 1 and y == 0:
        return 1.0 + (la * rho)
    if x == 0 and y == 1:
        return 1.0 + (lh * rho)
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0



# ============================================================
# Elo Dinámico — calculado desde historial de partidos de la API
# ============================================================
_ELO_CACHE = {"data": None, "ts": None}
ELO_CACHE_TTL = 3600  # 1 hora

async def calcular_elo_equipos(tabla: dict) -> dict:
    """
    Calcula ratings Elo para todos los equipos de LaLiga usando
    los partidos FINISHED de la temporada actual.
    K=32, Elo base=1500. Resultado cacheado 1 hora.
    Devuelve dict {team_id: elo_rating}.
    """
    ahora = datetime.now(timezone.utc)
    if (
        _ELO_CACHE["data"] and _ELO_CACHE["ts"] and
        (ahora - _ELO_CACHE["ts"]).total_seconds() < ELO_CACHE_TTL
    ):
        return _ELO_CACHE["data"]

    try:
        data = await api_football_call("matches?status=FINISHED")
        if not data or 'matches' not in data:
            return {}

        matches = sorted(data['matches'], key=lambda m: m['utcDate'])

        # Inicializar Elo base para todos los equipos conocidos en tabla
        elos = {tid: 1500.0 for tid in tabla}
        K = 32

        for m in matches:
            h_id = m['homeTeam']['id']
            a_id = m['awayTeam']['id']
            winner = m['score'].get('winner')
            if not winner or h_id not in elos or a_id not in elos:
                continue

            elo_h = elos[h_id]
            elo_a = elos[a_id]

            # Probabilidad esperada Elo
            exp_h = 1 / (1 + 10 ** ((elo_a - elo_h) / 400))
            exp_a = 1 - exp_h

            # Resultado real
            if winner == 'HOME_TEAM':
                s_h, s_a = 1.0, 0.0
            elif winner == 'AWAY_TEAM':
                s_h, s_a = 0.0, 1.0
            else:  # DRAW
                s_h, s_a = 0.5, 0.5

            elos[h_id] = elo_h + K * (s_h - exp_h)
            elos[a_id] = elo_a + K * (s_a - exp_a)

        _ELO_CACHE["data"] = elos
        _ELO_CACHE["ts"] = ahora
        logging.info(f"[Elo] Calculado para {len(elos)} equipos.")
        return elos

    except Exception as e:
        logging.error(f"[Elo] Error: {e}")
        return {}


def calcular_factor_elo(elo_local: float, elo_visita: float) -> tuple:
    """
    Convierte diferencia Elo en factor multiplicador para λH y λA.
    Máximo ajuste: ±8% (equivalente a ~200 puntos de diferencia).
    """
    MAX_AJUSTE = 0.08
    MAX_DIFF = 200.0

    diff = elo_local - elo_visita
    intensidad = max(-1.0, min(diff / MAX_DIFF, 1.0))
    ajuste = MAX_AJUSTE * intensidad

    factor_lh = round(1.0 + ajuste, 4)
    factor_la = round(1.0 - ajuste, 4)

    if abs(ajuste) < 0.01:
        texto = f"Elo ⚖️ Equilibrado ({elo_local:.0f} vs {elo_visita:.0f})"
    elif diff > 0:
        texto = f"Elo 📈 Local superior ({elo_local:.0f} vs {elo_visita:.0f}, +{ajuste*100:.1f}% lh)"
    else:
        texto = f"Elo 📉 Visita superior ({elo_local:.0f} vs {elo_visita:.0f}, +{abs(ajuste)*100:.1f}% la)"

    return factor_lh, factor_la, texto


def calcular_shin(odds_l, odds_e, odds_v):
    """
    Método Shin (1993): estima probabilidades verdaderas desde cuotas
    modelando el porcentaje de apostadores con información privilegiada (z).
    
    - z cercano a 0: mercado eficiente, poca info privilegiada
    - z cercano a 0.05+: mercado con insiders activos (más distorsión en cuotas altas)
    
    Devuelve (prob_l, prob_e, prob_v, z)
    """
    p_raw = [1 / odds_l, 1 / odds_e, 1 / odds_v]
    n = len(p_raw)
    overround = sum(p_raw)

    z = 0.0
    p_shin = p_raw[:]

    for _ in range(1000):
        p_shin_nuevo = []
        for p in p_raw:
            discriminante = z ** 2 + 4 * (1 - z) * (p / overround)
            discriminante = max(discriminante, 0.0)  # evitar raíz de negativo
            denom_shin = 2 * (1 - z)
            if denom_shin == 0:
                p_shin_nuevo.append(p / overround)
            else:
                p_shin_nuevo.append((discriminante ** 0.5 - z) / denom_shin)

        suma = sum(p_shin_nuevo)
        min_p = min(p_shin_nuevo)
        denominador = suma - n * min_p
        if denominador == 0:
            break
        z_nuevo = (suma - 1) / denominador
        z_nuevo = max(0.0, min(z_nuevo, 0.15))  # clamp: z ∈ [0, 0.15]
        if abs(z_nuevo - z) < 1e-9:
            p_shin = p_shin_nuevo
            break
        z = z_nuevo
        p_shin = p_shin_nuevo

    # Normalizar para garantizar suma = 1
    total = sum(p_shin)
    p_shin = [p / total for p in p_shin]
    return p_shin[0], p_shin[1], p_shin[2], z


def interpretar_shin(divergencia, z):
    """
    Interpreta la divergencia entre Shin y normalización simple.
    Devuelve (texto_confianza, factor_shin) que penaliza el edge si divergen.
    """
    if divergencia < 0.02:
        confianza = "✅ Alta (Shin≈Simple, señal sólida)"
        factor = 1.0
    elif divergencia < 0.04:
        confianza = "⚠️ Media (divergencia leve, cautela)"
        factor = 0.85
    else:
        confianza = "🚨 Baja (métodos divergen, señal débil)"
        factor = 0.70

    z_txt = "bajo (mercado eficiente)" if z < 0.02 else ("medio" if z < 0.04 else "alto (posibles insiders)")
    return confianza, factor, z_txt


# ============================================================

# --- Lógica de validación unificada ---
PICKS_VOID = ["no bet", "no apostar", "no apostar (sin valor)", "sin valor"]

def evaluar_resultado(pick, partido, home_name, away_name, winner):
    pick_lower = pick.lower()
    if any(v in pick_lower for v in PICKS_VOID):
        return "➖ VOID"
    if winner == 'HOME_TEAM' and home_name.lower() in pick_lower:
        return "✅ WIN"
    if winner == 'AWAY_TEAM' and away_name.lower() in pick_lower:
        return "✅ WIN"
    if winner == 'DRAW' and "empate" in pick_lower:
        return "✅ WIN"
    return "❌ LOSS"

# --- Comando Principal: Pronóstico V10 ---
@bot.message_handler(commands=['pronostico', 'valor'])
async def handle_pronostico(message):
    if not SISTEMA_IA["estratega"]["nodo"]:
        await bot.reply_to(message, "🚨 Configura los nodos con `/config`."); return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or " vs " not in parts[1]:
        await bot.reply_to(message, "⚠️ `/pronostico Local vs Visitante`."); return

    l_q, v_q = [t.strip() for t in parts[1].split(" vs ")]
    msg_espera = await bot.reply_to(message, "📡 Ejecutando Análisis V11 (Poisson+DC+H2H+Forma+Tabla+Elo+Odds+Shin)...")

    full_data, check_json = await obtener_modelo()
    if not full_data:
        await bot.edit_message_text("❌ Error al cargar el JSON del servidor.", message.chat.id, msg_espera.message_id); return

    liga = next(iter(full_data))
    m_l = next((t for t in full_data[liga]['teams'] if t.lower() in l_q.lower() or l_q.lower() in t.lower()), None)
    m_v = next((t for t in full_data[liga]['teams'] if t.lower() in v_q.lower() or v_q.lower() in t.lower()), None)

    if not m_l or not m_v:
        await bot.edit_message_text("❌ Equipo no encontrado en el JSON.", message.chat.id, msg_espera.message_id); return

    l_s, v_s = full_data[liga]['teams'][m_l], full_data[liga]['teams'][m_v]
    id_l = l_s.get("id_api")
    id_v = v_s.get("id_api")
    logging.info(f"[H2H] id_l={id_l} | id_v={id_v} | equipo_l={m_l} | equipo_v={m_v}")

    # Todas las consultas externas EN PARALELO
    (
        (c_l, c_e, c_v, check_odds),
        (contexto_noticias, penalty_local, penalty_visita),
        factor_calibracion,
        (h2h, check_h2h, home_wins, away_wins, total_h2h),
        (forma_local_atk, forma_local_def, forma_local_txt),
        (forma_visita_atk, forma_visita_def, forma_visita_txt),
        tabla
    ) = await asyncio.gather(
        obtener_datos_mercado(l_q),
        obtener_contexto_real(l_q, v_q),
        obtener_factor_calibracion(),
        obtener_h2h_directo(id_l, id_v),
        obtener_forma_reciente(id_l),
        obtener_forma_reciente(id_v),
        obtener_posiciones_tabla()
    )

    # Elo requiere tabla ya calculada, se ejecuta después
    elos = await calcular_elo_equipos(tabla)

    # --- PASO 1: Lambdas base desde Poisson ---
    avg = full_data[liga]['averages']
    lh_base = l_s['att_h'] * v_s['def_a'] * avg['league_home']
    la_base = v_s['att_a'] * l_s['def_h'] * avg['league_away']

    # --- PASO 2: Ajuste H2H (sede-corregido) ---
    factor_lh_h2h, factor_la_h2h, h2h_texto = calcular_factor_h2h(home_wins, away_wins, total_h2h)

    # --- PASO 3: Ajuste forma reciente ---
    factor_lh_forma = forma_local_atk
    factor_la_forma = forma_visita_atk

    # --- PASO 4: Ajuste tabla ---
    factor_lh_tabla, factor_la_tabla, tabla_texto = 1.0, 1.0, "Tabla: Sin datos"
    if tabla and id_l in tabla and id_v in tabla:
        t_l = tabla[id_l]
        t_v = tabla[id_v]
        factor_lh_tabla, factor_la_tabla, tabla_texto = calcular_factor_tabla(
            t_l['pos'], t_v['pos'], t_l['puntos'], t_v['puntos']
        )

    # --- PASO 4b: Ajuste Elo dinámico ---
    factor_lh_elo, factor_la_elo, elo_texto = 1.0, 1.0, "Elo: Sin datos"
    if elos and id_l in elos and id_v in elos:
        factor_lh_elo, factor_la_elo, elo_texto = calcular_factor_elo(elos[id_l], elos[id_v])

    # --- PASO 5: Combinar todos los factores sobre las lambdas ---
    lh = lh_base * factor_lh_h2h * factor_lh_forma * factor_lh_tabla * factor_lh_elo * penalty_local
    la = la_base * factor_la_h2h * factor_la_forma * factor_la_tabla * factor_la_elo * penalty_visita

    # --- PASO 6: Probabilidad Poisson 7x7 + Dixon-Coles ---
    prob_poisson = 0
    for x in range(7):
        for y in range(7):
            p = poisson.pmf(x, lh) * poisson.pmf(y, la) * dixon_coles_tau(x, y, lh, la)
            if x > y:
                prob_poisson += p

    # --- PASO 7: Calibración histórica ---
    prob_poisson_calibrado = prob_poisson * factor_calibracion

    # --- PASO 8: Normalización simple + Shin (validación cruzada) ---
    overround = (1 / c_l) + (1 / c_e) + (1 / c_v)
    prob_market_simple = (1 / c_l) / overround  # normalización simple

    # Shin: redistribuye considerando info privilegiada en el mercado
    shin_l, shin_e, shin_v, shin_z = calcular_shin(c_l, c_e, c_v)

    # Divergencia entre métodos → señal de confianza
    divergencia_shin = abs(shin_l - prob_market_simple)
    shin_confianza, shin_factor, shin_z_txt = interpretar_shin(divergencia_shin, shin_z)

    # Prob mercado final = promedio de ambos métodos
    prob_market = (prob_market_simple + shin_l) / 2

    # Mezcla final: Poisson(90%) + Mercado promedio(10%)
    p_win = (prob_poisson_calibrado * 0.90) + (prob_market * 0.10)
    p_percent = p_win * 100

    # --- PASO 9: Over/Under como confirmación del stake ---
    ou_factor, ou_texto = await obtener_confirmacion_ou(l_q, lh, la)

    # --- PASO 10: Probabilidades Poisson para los 3 resultados ---
    prob_poisson_empate = 0
    prob_poisson_visita = 0
    for x in range(7):
        for y in range(7):
            p = poisson.pmf(x, lh) * poisson.pmf(y, la) * dixon_coles_tau(x, y, lh, la)
            if x == y:
                prob_poisson_empate += p
            elif y > x:
                prob_poisson_visita += p

    prob_poisson_calibrado_local   = prob_poisson * factor_calibracion
    prob_poisson_empate_cal        = prob_poisson_empate * factor_calibracion
    prob_poisson_visita_cal        = prob_poisson_visita * factor_calibracion

    # Sobrescribir p_win con la versión nombrada correctamente
    p_win = prob_poisson_calibrado_local

    prob_market_l = (prob_market_simple + shin_l) / 2   # ya calculado antes como prob_market
    prob_market_e = (shin_e + (1/c_e)/overround) / 2
    prob_market_v = (shin_v + (1/c_v)/overround) / 2

    margen_error = 0.005

    # --- Edge bruto para los 3 resultados ---
    edge_local_raw  = (prob_poisson_calibrado_local - prob_market_l - margen_error) * shin_factor
    edge_empate     = (prob_poisson_empate_cal - prob_market_e - margen_error) * shin_factor
    edge_visita     = (prob_poisson_visita_cal - prob_market_v - margen_error) * shin_factor

    # --- Filtros adicionales sobre edge local ---
    edge_ajustado = edge_local_raw
    # Filtro cuotas trampa en mercado equilibrado
    if 1.90 <= c_l <= 2.10 and edge_ajustado < 0.02:
        edge_ajustado = -0.001
    # Penalización si cuota de empate es muy baja (partido abierto)
    if c_e < 3.0 and edge_ajustado > 0:
        edge_ajustado *= 0.80
        empate_aviso = f"⚠️ Cuota empate baja ({c_e:.2f}) → edge local reducido 20%"
    else:
        empate_aviso = f"Cuota empate: {c_e:.2f} ✅"

    # --- Filtros equivalentes para visitante ---
    if 1.90 <= c_v <= 2.10 and edge_visita < 0.02:
        edge_visita = -0.001
    # No aplicar penalización c_e < 3.0 al visitante: ese filtro es solo para el local

    # --- Selección del pick principal: el resultado con mayor edge positivo ---
    candidatos = []
    if edge_ajustado > 0:
        candidatos.append(("local",   edge_ajustado, c_l, m_l,     prob_poisson_calibrado_local * 100))
    if edge_empate > 0:
        candidatos.append(("empate",  edge_empate,   c_e, "Empate", prob_poisson_empate_cal * 100))
    if edge_visita > 0:
        candidatos.append(("visita",  edge_visita,   c_v, m_v,      prob_poisson_visita_cal * 100))

    pick_riesgo_nombre = None
    stake_riesgo       = 0
    nivel_riesgo       = ""
    edge_riesgo        = 0
    prob_riesgo        = 0
    pick_riesgo_cuota  = 0

    if candidatos:
        # Ordenar por edge descendente; el primero es el pick principal
        candidatos.sort(key=lambda c: c[1], reverse=True)
        tipo_pick, edge_principal, cuota_pick, nombre_pick, prob_pick_pct = candidatos[0]

        kelly_full       = edge_principal / (cuota_pick - 1)
        kelly_fraccionado = kelly_full * 0.25
        stake_base       = round(kelly_fraccionado * 100, 2)
        stake            = round(stake_base * ou_factor, 2)
        stake            = max(0.25, min(stake, 3.0))
        pick_final       = nombre_pick
        p_percent        = prob_pick_pct

        if stake < 0.75:
            nivel = "BRONCE 🥉"
        elif stake < 1.25:
            nivel = "PLATA 🥈"
        elif stake < 2.0:
            nivel = "ORO 🥇"
        else:
            nivel = "DIAMANTE 💎"

        # Si hay un segundo candidato, mostrarlo como pick de riesgo
        if len(candidatos) > 1:
            _, edge_riesgo, pick_riesgo_cuota, pick_riesgo_nombre, prob_riesgo = candidatos[1]
            kelly_riesgo  = (edge_riesgo / (pick_riesgo_cuota - 1)) * 0.25
            stake_riesgo  = round(min(max(kelly_riesgo * 100, 0.25), 1.0), 2)
            if stake_riesgo < 0.50:
                nivel_riesgo = "RIESGO BAJO ⚠️"
            elif stake_riesgo < 0.75:
                nivel_riesgo = "RIESGO MEDIO 🎲"
            else:
                nivel_riesgo = "RIESGO ALTO 🔴"
    else:
        # Sin valor en ningún resultado
        nivel, stake, pick_final = "NO BET 🚫", 0, "No Bet"
        ou_factor  = 1.0
        tipo_pick  = "ninguno"
        cuota_pick = 0
        prob_pick_pct = 0
        edge_principal = 0
        nombre_pick = "No Bet"

    # --- Guardado en Historial ---
    fecha_hoy = (datetime.now(timezone.utc) + timedelta(hours=OFFSET_JUAREZ)).strftime('%Y-%m-%d %H:%M')
    registro = {
        "fecha": fecha_hoy,
        "partido": f"{m_l} vs {m_v}",
        "pick": pick_final,
        "poisson": f"{p_percent:.1f}%",
        "cuota": cuota_pick if pick_final != "No Bet" else c_l,
        "edge": f"{edge_principal*100:.1f}%" if pick_final != "No Bet" else "0.0%",
        "stake": f"{stake}%",
        "nivel": nivel,
        "pick_riesgo": pick_riesgo_nombre if pick_riesgo_nombre else "Sin valor alternativo",
        "stake_riesgo": f"{stake_riesgo}%" if stake_riesgo else "0%",
        "status": "⏳ PENDIENTE"
    }

    async def task_github():
        try:
            logging.info(f"[GitHub] Guardando historial: {registro['partido']} → {registro['pick']}")
            await guardar_en_github(nuevo_registro=registro)
        except Exception as e:
            logging.error(f"[GitHub] task_github excepción: {e}", exc_info=True)

    clave_partido = f"{m_l}_vs_{m_v}_{fecha_hoy[:10]}"
    ahora = datetime.now(timezone.utc)

    # Cooldown en RAM (evita duplicados en la misma sesión)
    ya_en_ram = (
        clave_partido in COOLDOWN and
        (ahora - COOLDOWN[clave_partido]).total_seconds() < COOLDOWN_MINUTOS * 60
    )

    # Cooldown persistente: verificar si ya existe en GitHub
    async def ya_en_github():
        try:
            url_hist = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}"
            r = await asyncio.to_thread(requests.get, url_hist, timeout=10)
            if r.status_code != 200:
                return False
            historial_gh = r.json()
            fecha_hoy_str = fecha_hoy[:10]
            partido_key = f"{m_l} vs {m_v}"
            return any(
                h.get("partido") == partido_key and h.get("fecha", "")[:10] == fecha_hoy_str
                for h in historial_gh
            )
        except:
            return False

    if ya_en_ram:
        logging.info(f"[Cooldown RAM] Ignorando duplicado para {clave_partido}")
    else:
        duplicado_github = await ya_en_github()
        if duplicado_github:
            logging.info(f"[Cooldown GitHub] Ya existe registro para {clave_partido}, omitiendo guardado.")
        else:
            COOLDOWN[clave_partido] = ahora
            asyncio.create_task(task_github())

    # --- Construir textos resumen de ajustes ---
    calib_txt = f"{factor_calibracion:.2f}" if factor_calibracion != 1.0 else "1.00 (sin datos suficientes)"
    h2h_ajuste_txt = f"+{(factor_lh_h2h-1)*100:.1f}%lh" if factor_lh_h2h != 1.0 else (f"+{(factor_la_h2h-1)*100:.1f}%la" if factor_la_h2h != 1.0 else "sin ajuste")
    serper_txt = ""
    if penalty_local < 1.0: serper_txt += f" ⚠️ Bajas local (-{(1-penalty_local)*100:.0f}%lh)"
    if penalty_visita < 1.0: serper_txt += f" ⚠️ Bajas visita (-{(1-penalty_visita)*100:.0f}%la)"
    if not serper_txt: serper_txt = " Sin bajas detectadas"

    # Bloque de decisión: lo primero que ve el usuario
    tipo_emoji = {"local": "🏠", "empate": "🤝", "visita": "🚩"}.get(tipo_pick if pick_final != "No Bet" else "", "")
    if pick_final == "No Bet":
        if pick_riesgo_nombre:
            decision_block = (
                f"<b>╔{'═'*22}╗</b>\n"
                f"<b>║  🚫 NO BET — SIN VALOR      ║</b>\n"
                f"<b>╠{'═'*22}╣</b>\n"
                f"<b>║  {nivel_riesgo:<22}║</b>\n"
                f"<b>║  🎲 {pick_riesgo_nombre:<20}║</b>\n"
                f"<b>║  💰 Stake: {stake_riesgo}% (riesgo){' '*(8-len(str(stake_riesgo)))}║</b>\n"
                f"<b>║  📈 Prob: {prob_riesgo:.1f}%  Edge: {edge_riesgo*100:.1f}%{' '*(6-len(f'{prob_riesgo:.1f}'))}║</b>\n"
                f"<b>╚{'═'*22}╝</b>\n"
            )
        else:
            decision_block = (
                f"<b>╔{'═'*22}╗</b>\n"
                f"<b>║   🚫  NO BET  —  SIN VALOR   ║</b>\n"
                f"<b>╚{'═'*22}╝</b>\n"
            )
    else:
        decision_block = (
            f"<b>╔{'═'*22}╗</b>\n"
            f"<b>║  {nivel:<22}║</b>\n"
            f"<b>║  {tipo_emoji} {pick_final:<20}║</b>\n"
            f"<b>║  💰 Stake: {stake}% del bankroll{' '*(9-len(str(stake)))}║</b>\n"
            f"<b>║  📈 Prob: {p_percent:.1f}%  Edge: {edge_principal*100:.1f}%{' '*(6-len(f'{p_percent:.1f}'))}║</b>\n"
            f"<b>╚{'═'*22}╝</b>\n"
        )

    # Porcentajes Poisson para el bloque de señales
    p_local_pct   = prob_poisson_calibrado_local * 100
    p_empate_pct  = prob_poisson_empate_cal * 100
    p_visita_pct  = prob_poisson_visita_cal * 100

    # Bloque de señales técnicas compacto
    signals_block = (
        f"\n<b>◆ SEÑALES</b>\n"
        f"<code>"
        f"Poisson  🏠 {p_local_pct:.1f}%  🤝 {p_empate_pct:.1f}%  🚩 {p_visita_pct:.1f}%\n"
        f"λH {lh:.2f}  λA {la:.2f}\n"
        f"Edge     🏠 {edge_ajustado*100:.1f}%  🤝 {edge_empate*100:.1f}%  🚩 {edge_visita*100:.1f}%\n"
        f"Mercado  Simple {prob_market_simple*100:.1f}%  Shin {shin_l*100:.1f}%\n"
        f"Shin z   {shin_z:.4f}  {shin_confianza[:22]}\n"
        f"Cuotas   L {c_l}  E {c_e}  V {c_v}  OR {overround:.3f}  {'(consenso)' if check_odds else '(default)'}\n"
        f"Calib    ×{calib_txt}  {ou_texto[:28]}\n"
        f"Empate   {empate_aviso[:38]}"
        f"</code>\n"
    )

    # Bloque de contexto deportivo
    context_block = (
        f"\n<b>◆ CONTEXTO</b>\n"
        f"<b>H2H</b> {h2h_texto} → {h2h_ajuste_txt}\n"
        f"<b>🏠</b> {forma_local_txt}\n"
        f"<b>🚩</b> {forma_visita_txt}\n"
        f"<b>🏆</b> {tabla_texto}\n"
        f"<b>⚡</b> {elo_texto}\n"
        f"<b>📰</b>{serper_txt}\n"
        f"\n<b>◆ ANÁLISIS  {'✅' if check_odds else '❌'} Odds · {'✅' if check_json else '❌'} Poisson · {'✅' if check_h2h else '❌'} H2H</b>\n"
    )

    header = decision_block + signals_block + context_block

    # ============================================================
    # PROMPT ESTRATEGA — Todos los datos disponibles
    # ============================================================
    prompt_e = f"""
Eres analista profesional de fútbol. Tu misión es evaluar si existe valor real en alguno de los tres resultados posibles del partido.

═══════════════════════════════════════
PARTIDO: {m_l} vs {m_v}
═══════════════════════════════════════

── MODELO POISSON + DIXON-COLES ──
• Prob. victoria local (modelo): {p_local_pct:.1f}%  | λH: {lh:.2f} goles esperados
• Prob. empate (modelo): {p_empate_pct:.1f}%
• Prob. victoria visitante (modelo): {p_visita_pct:.1f}%  | λA: {la:.2f} goles esperados
• Lambda local BASE (sin ajustes): {lh_base:.2f}
• Lambda visitante BASE (sin ajustes): {la_base:.2f}
• Factor calibración histórica: ×{factor_calibracion:.2f} {'(modelo sobreestima)' if factor_calibracion < 1 else '(modelo subestima)' if factor_calibracion > 1 else '(sin datos aún)'}

── MERCADO DE CUOTAS ──
• Cuota local: {c_l} → prob. implícita bruta: {(1/c_l)*100:.1f}%
• Cuota empate: {c_e} → prob. implícita bruta: {(1/c_e)*100:.1f}%
• Cuota visitante: {c_v} → prob. implícita bruta: {(1/c_v)*100:.1f}%
• Overround (margen casa): {overround:.4f} ({(overround-1)*100:.2f}% de margen)

── MÉTODO SHIN (1993) vs NORMALIZACIÓN SIMPLE ──
• Prob. local normalización simple: {prob_market_simple*100:.1f}%
• Prob. local método Shin: {shin_l*100:.1f}%
• Prob. empate método Shin: {shin_e*100:.1f}%
• Prob. visita método Shin: {shin_v*100:.1f}%
• Parámetro z (info privilegiada): {shin_z:.4f} → nivel {shin_z_txt}
• Divergencia entre métodos: {divergencia_shin*100:.2f}%
• Confianza de señal: {shin_confianza}
• Factor Shin aplicado al edge: ×{shin_factor:.2f}

── EDGE (modelo vs mercado, ajustado) ──
• Edge local:     {edge_ajustado*100:.2f}%
• Edge empate:    {edge_empate*100:.2f}%
• Edge visitante: {edge_visita*100:.2f}%

── PICK SELECCIONADO (mayor edge positivo) ──
• PICK PRINCIPAL: {pick_final}
• Nivel: {nivel}
• Stake Kelly: {stake}% del bankroll
• Señal Over/Under: {ou_texto}
• {empate_aviso}

── HISTORIAL H2H (SEDE-CORREGIDO) ──
• Resultado: {h2h}
• Victorias local en casa: {home_wins} | Victorias visita fuera: {away_wins} | Total analizados: {total_h2h}
• Ajuste aplicado: {h2h_ajuste_txt}

── FORMA RECIENTE (ÚLTIMOS 5 PARTIDOS) ──
• Local: {forma_local_txt} | Factor ataque: ×{forma_local_atk:.3f} | Factor defensa: ×{forma_local_def:.3f}
• Visita: {forma_visita_txt} | Factor ataque: ×{forma_visita_atk:.3f} | Factor defensa: ×{forma_visita_def:.3f}

── POSICIÓN EN TABLA ──
• {tabla_texto}

── ELO DINÁMICO (temporada actual) ──
• {elo_texto}
• Factor Elo local: ×{factor_lh_elo:.4f} | Factor Elo visita: ×{factor_la_elo:.4f}
• Elo local: {elos.get(id_l, 'N/A'):.0f} pts | Elo visita: {elos.get(id_v, 'N/A'):.0f} pts

── BAJAS Y NOTICIAS (SERPER) ──
•{serper_txt}
• Factor penalización local: ×{penalty_local:.2f} | Factor penalización visita: ×{penalty_visita:.2f}

── PICK DE RIESGO ALTERNATIVO ──
• Pick de riesgo: {pick_riesgo_nombre if pick_riesgo_nombre else "Sin valor alternativo"}
• Cuota: {pick_riesgo_cuota if pick_riesgo_cuota else "N/A"}
• Stake sugerido: {stake_riesgo}% (cap 1.0%, Kelly×0.25)

═══════════════════════════════════════
RESULTADO DEL MODELO
🎯 PICK PRINCIPAL: {pick_final}
📈 NIVEL: {nivel}
💰 STAKE Kelly: {stake}% del bankroll
🎲 PICK RIESGO: {pick_riesgo_nombre if pick_riesgo_nombre else "Sin valor alternativo"}
═══════════════════════════════════════

INSTRUCCIONES PARA TU ANÁLISIS:
1. El sistema ya evaluó los tres resultados (local, empate, visitante) y eligió el de mayor edge positivo como pick principal.
   - Si el pick es "No Bet", todos los edges son negativos → no hay valor en ningún resultado.
   - Si hay pick de riesgo, menciona brevemente por qué puede ser interesante pero aclara su mayor riesgo.
2. Redacta un análisis de máximo 130 palabras que integre:
   a) Por qué el pick seleccionado tiene valor (edge modelo vs mercado).
   b) Si Shin y normalización simple coinciden o divergen, y qué implica.
   c) Si la forma reciente y la tabla refuerzan o contradicen el pronóstico.
   d) Si el H2H en sede favorece al resultado elegido.
   e) Si hay bajas relevantes y cómo afectan las lambdas.
   f) Si el O/U confirma o contradice la apuesta.
3. Sé directo, técnico y conciso. No repitas los números exactos del header, interprétalos.
"""

    analisis_raw = await ejecutar_ia("estratega", prompt_e)
    analisis = html.escape(analisis_raw)

    nodos_txt = f"🛰 <code>{SISTEMA_IA['estratega']['api']}</code>"

    if SISTEMA_IA["auditor"]["nodo"]:
        prompt_a = (
            f"ERES AUDITOR. Valida este análisis: '{analisis_raw}'\n"
            f"NOTICIAS RECIENTES:\n{contexto_noticias}\n"
            f"H2H (sede-corregido): {h2h} | Wins local: {home_wins} | Wins visita: {away_wins}\n"
            f"Forma local: {forma_local_txt} (atk ×{forma_local_atk:.3f})\n"
            f"Forma visita: {forma_visita_txt} (atk ×{forma_visita_atk:.3f})\n"
            f"Tabla: {tabla_texto}\n"
            f"Shin z={shin_z:.4f} | Divergencia={divergencia_shin*100:.2f}% | Confianza: {shin_confianza}\n"
            f"Edge ajustado: {edge_ajustado*100:.2f}% | Stake: {stake}%\n"
            f"¿Hay alguna contradicción entre el análisis y los datos? Resumen muy breve (máx 60 palabras)."
        )
        auditoria_raw = await ejecutar_ia("auditor", prompt_a)
        nodos_txt += f" · 🛡 <code>{SISTEMA_IA['auditor']['api']}</code>"
        auditor_block = f"\n\n<b>◆ AUDITOR</b>\n{html.escape(auditoria_raw)}"
    else:
        auditor_block = ""

    footer = f"\n\n<i>{'—'*18}\nV11 · {nodos_txt}</i>"
    final = f"{header}{analisis}{auditor_block}{footer}"

    await bot.edit_message_text(final, message.chat.id, msg_espera.message_id, parse_mode='HTML')

# --- Comandos Adicionales ---

@bot.message_handler(commands=['stats'])
async def cmd_stats(message):
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}"
    try:
        r = await asyncio.to_thread(requests.get, url, timeout=10)
        historial = r.json()
        if not historial:
            await bot.reply_to(message, "📭 Sin historial."); return

        completados = [h for h in historial if h.get('status') in ('✅ WIN', '❌ LOSS')]
        voided = [h for h in historial if h.get('status') == '➖ VOID']
        pendientes = [h for h in historial if h.get('status') == '⏳ PENDIENTE']

        if not completados:
            await bot.reply_to(message, "📊 Sin resultados completos aún."); return

        wins = sum(1 for h in completados if h['status'] == '✅ WIN')
        losses = len(completados) - wins
        pct_aciertos = (wins / len(completados)) * 100

        roi_total = 0
        invertido_total = 0
        for h in completados:
            stake_val = float(str(h.get('stake', '0')).replace('%', ''))
            cuota_val = float(h.get('cuota', 1.0))
            if stake_val > 0:
                invertido_total += stake_val
                if h['status'] == '✅ WIN':
                    roi_total += stake_val * (cuota_val - 1)
                else:
                    roi_total -= stake_val
        roi_pct = (roi_total / invertido_total * 100) if invertido_total > 0 else 0

        racha = 0
        racha_tipo = ""
        for h in reversed(completados):
            if racha == 0:
                racha_tipo = h['status']
                racha = 1
            elif h['status'] == racha_tipo:
                racha += 1
            else:
                break
        racha_emoji = "🔥" if racha_tipo == "✅ WIN" else "❄️"

        niveles_stats = {}
        for h in completados:
            niv = h.get('nivel', 'Desconocido').split(' ')[0]
            if niv not in niveles_stats:
                niveles_stats[niv] = {'w': 0, 'l': 0}
            if h['status'] == '✅ WIN':
                niveles_stats[niv]['w'] += 1
            else:
                niveles_stats[niv]['l'] += 1

        desglose = ""
        for niv, datos in niveles_stats.items():
            total_niv = datos['w'] + datos['l']
            pct_niv = (datos['w'] / total_niv * 100) if total_niv > 0 else 0
            desglose += f"  • {niv}: {datos['w']}W/{datos['l']}L ({pct_niv:.0f}%)\n"

        txt = (
            f"📊 <b>ESTADÍSTICAS DEL BOT</b>\n"
            f"{'━'*22}\n"
            f"📈 <b>Aciertos:</b> {wins}/{len(completados)} ({pct_aciertos:.1f}%)\n"
            f"💰 <b>ROI:</b> {roi_pct:+.2f}%\n"
            f"{racha_emoji} <b>Racha actual:</b> {racha} {'WIN' if racha_tipo == '✅ WIN' else 'LOSS'} consecutivos\n"
            f"➖ <b>VOIDs:</b> {len(voided)} | ⏳ <b>Pendientes:</b> {len(pendientes)}\n"
            f"{'━'*22}\n"
            f"<b>Desglose por nivel:</b>\n{desglose}"
        )
        await bot.reply_to(message, txt, parse_mode='HTML')
    except Exception as e:
        logging.error(f"Error /stats: {e}")
        await bot.reply_to(message, "❌ Error al calcular estadísticas.")

@bot.message_handler(commands=['historial'])
async def cmd_historial(message):
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}"
    try:
        r = await asyncio.to_thread(requests.get, url, timeout=10)
        historial = r.json()
        if not historial:
            await bot.reply_to(message, "📭 Historial vacío."); return
        txt = "📜 <b>HISTORIAL RECIENTE:</b>\n\n"
        for r_item in historial[-10:]:
            txt += f"📅 <code>{r_item['fecha']}</code>\n⚽ <b>{r_item['partido']}</b>\n🎯 Pick: <code>{r_item['pick']}</code> | {r_item['status']}\n{'—'*15}\n"
        await bot.reply_to(message, txt, parse_mode='HTML')
    except: await bot.reply_to(message, "❌ Error al leer historial.")

@bot.message_handler(commands=['validar'])
async def cmd_validar(message):
    msg_espera = await bot.reply_to(message, "🔍 Sincronizando resultados...")
    url_h = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}"
    try:
        r_hist = await asyncio.to_thread(requests.get, url_h, timeout=10)
        if r_hist.status_code != 200:
            await bot.edit_message_text("❌ No se pudo leer el historial.", message.chat.id, msg_espera.message_id); return
        historial_raw = r_hist.json()

        data_api = await api_football_call("matches?status=FINISHED")
        if not data_api or 'matches' not in data_api:
            await bot.edit_message_text("❌ Sin resultados desde la API.", message.chat.id, msg_espera.message_id); return

        count = 0
        for item in historial_raw:
            if item.get("status") != "⏳ PENDIENTE":
                continue
            partido_lower = item['partido'].lower()
            for m in data_api['matches']:
                h_api = m['homeTeam']['name'].lower()
                a_api = m['awayTeam']['name'].lower()
                # Match parcial: busca si el nombre del equipo está contenido
                local_match = any(p in h_api or h_api in p for p in partido_lower.split(" vs ")[0:1])
                visita_match = any(p in a_api or a_api in p for p in partido_lower.split(" vs ")[1:2])
                if local_match and visita_match:
                    winner = m['score'].get('winner')
                    if not winner:
                        continue  # partido sin resultado aún
                    item['status'] = evaluar_resultado(
                        item['pick'], item['partido'], h_api, a_api, winner
                    )
                    item['marcador_real'] = (
                        f"{m['score']['fullTime']['home']}-{m['score']['fullTime']['away']}"
                    )
                    count += 1
                    break  # evitar doble match

        if count > 0:
            await guardar_en_github(historial_completo=historial_raw)
            await bot.edit_message_text(
                f"✅ {count} partido(s) validado(s) y guardados.", message.chat.id, msg_espera.message_id
            )
        else:
            await bot.edit_message_text("ℹ️ No hay partidos nuevos por actualizar.", message.chat.id, msg_espera.message_id)
    except Exception as e:
        logging.error(f"Error /validar: {e}", exc_info=True)
        await bot.edit_message_text(f"❌ Fallo en validación: {str(e)[:80]}", message.chat.id, msg_espera.message_id)

@bot.message_handler(commands=['partidos'])
async def cmd_partidos(message):
    from collections import defaultdict
    data = await api_football_call("matches?status=SCHEDULED")
    if not data: return

    matches = data['matches'][:10]
    if not matches:
        await bot.reply_to(message, "📭 No hay partidos programados."); return

    dias_es = {
        'Monday': 'Lunes', 'Tuesday': 'Martes', 'Wednesday': 'Miércoles',
        'Thursday': 'Jueves', 'Friday': 'Viernes', 'Saturday': 'Sábado', 'Sunday': 'Domingo'
    }

    por_fecha = defaultdict(list)
    for m in matches:
        dt = datetime.strptime(m['utcDate'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(hours=OFFSET_JUAREZ)
        fecha_key = dt.strftime('%A %d/%m')
        for en, es in dias_es.items():
            fecha_key = fecha_key.replace(en, es)
        por_fecha[fecha_key].append((dt, m))

    txt = "⚽ <b>PRÓXIMOS PARTIDOS</b>  <i>· hora Juárez</i>\n"
    for fecha_key, partidos in por_fecha.items():
        txt += f"\n<b>── {fecha_key} ──</b>\n"
        for dt, m in partidos:
            home = m['homeTeam']['shortName']
            away = m['awayTeam']['shortName']
            txt += f"<code>{dt.strftime('%H:%M')}</code>  <b>{home}</b> <i>vs</i> <b>{away}</b>\n"

    ejemplo = f"{matches[0]['homeTeam']['shortName']} vs {matches[0]['awayTeam']['shortName']}"
    txt += f"\n<i>Ej: /pronostico {ejemplo}</i>"
    await bot.reply_to(message, txt, parse_mode='HTML')

@bot.message_handler(commands=['tabla'])
async def cmd_tabla(message):
    data = await api_football_call("standings")
    if not data: return
    txt = "🏆 <b>POSICIONES:</b>\n\n"
    for t in data['standings'][0]['table'][:12]:
        txt += f"<code>{t['position']:02d}.</code> <b>{t['team']['shortName']}</b> | {t['points']} pts\n"
    await bot.reply_to(message, txt, parse_mode='HTML')

@bot.message_handler(commands=['equipos'])
async def cmd_equipos(message):
    r = await asyncio.to_thread(requests.get, URL_JSON, timeout=10)
    res = r.json()
    liga = next(iter(res))
    equipos = ", ".join([f"<code>{e}</code>" for e in res[liga]['teams'].keys()])
    await bot.reply_to(message, f"📋 <b>EQUIPOS JSON:</b>\n\n{equipos}", parse_mode='HTML')

@bot.message_handler(commands=['config'])
async def cmd_config(message):
    markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🧠 ASIGNAR ESTRATEGA", callback_data="set_rol_estratega"))
    await bot.reply_to(message, "🛠 <b>CONFIGURACIÓN DE RED</b>", reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith('set_rol_'))
async def cb_rol(call):
    rol = call.data.split('_')[-1]
    markup = InlineKeyboardMarkup().row(
        InlineKeyboardButton("Groq", callback_data=f"set_api_{rol}_GROQ"),
        InlineKeyboardButton("SambaNova", callback_data=f"set_api_{rol}_SAMBA")
    )
    await bot.edit_message_text(f"API para {rol.upper()}:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('set_api_'))
async def cb_api(call):
    _, _, rol, api = call.data.split('_')
    nodos = SISTEMA_IA["nodos_groq"] if api == 'GROQ' else SISTEMA_IA["nodos_samba"]
    markup = InlineKeyboardMarkup()
    for idx, nombre in enumerate(nodos):
        markup.add(InlineKeyboardButton(nombre, callback_data=f"sv_{rol[0]}_{api[0]}_{idx}"))
    await bot.edit_message_text(f"Selecciona Nodo para {rol.upper()}:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('sv_'))
async def cb_save(call):
    _, r_init, a_init, idx = call.data.split('_')
    rol = "estratega" if r_init == 'e' else "auditor"
    api = "GROQ" if a_init == 'G' else "SAMBA"
    lista = SISTEMA_IA["nodos_groq"] if api == "GROQ" else SISTEMA_IA["nodos_samba"]
    nodo_sel = lista[int(idx)]
    SISTEMA_IA[rol] = {"api": api, "nodo": nodo_sel}
    markup = InlineKeyboardMarkup()
    if rol == "estratega": markup.add(InlineKeyboardButton("⚖️ AÑADIR AUDITOR", callback_data="set_rol_auditor"))
    markup.add(InlineKeyboardButton("🏁 FINALIZAR", callback_data="config_fin"))
    await bot.edit_message_text(f"✅ {rol.upper()} listo: <code>{nodo_sel}</code>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data == "config_fin")
async def cb_fin(call):
    await bot.edit_message_text("🚀 <b>SISTEMA LISTO</b>", call.message.chat.id, call.message.message_id, parse_mode='HTML')

@bot.message_handler(commands=['help'])
async def cmd_help(message):
    help_text = (
        "🤖 <b>SISTEMA V10.0 PRO</b>\n\n"
        "📈 <b>ANÁLISIS:</b>\n"
        "• <code>/pronostico Local vs Visitante</code>: Poisson 7x7 + Dixon-Coles + H2H-Sede + Forma + Tabla + Odds + Shin + Kelly.\n"
        "• <code>/historial</code>: Últimos pronósticos.\n"
        "• <code>/stats</code>: ROI, % aciertos, racha y desglose por nivel.\n"
        "• <code>/validar</code>: Sincroniza resultados GitHub.\n"
        "• <code>/config</code>: Configura IA.\n\n"
        "🛡 <b>ROLES:</b>\n"
        "• <b>[EST]:</b> Estratega (Análisis matemático y Kelly).\n"
        "• <b>[AUD]:</b> Auditor (Redacción y verificación lógica).\n\n"
        "⚽ <b>INFORMACIÓN:</b>\n"
        "• <code>/partidos</code>: Próximos encuentros.\n"
        "• <code>/tabla</code>: Posiciones liga.\n"
        "• <code>/equipos</code>: Lista equipos JSON.\n"
    )
    await bot.reply_to(message, help_text, parse_mode='HTML')

async def main(): 
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.polling(non_stop=True)

if __name__ == "__main__": 
    asyncio.run(main())
