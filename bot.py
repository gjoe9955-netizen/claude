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
# MEJORA 5: Serper ahora devuelve también un factor de penalización numérico
PALABRAS_BAJA_LOCAL = ["baja", "lesión", "lesionado", "no jugará", "ausente", "descartado", "out"]
PALABRAS_BAJA_VISITA = PALABRAS_BAJA_LOCAL  # misma lista

async def obtener_contexto_real(l_q, v_q):
    """
    Devuelve (texto_noticias, factor_penalty_local, factor_penalty_visita).
    - factor_penalty_local: 0.95 si se detectan bajas del equipo local, 1.0 si no.
    - factor_penalty_visita: 0.95 si se detectan bajas del visitante, 1.0 si no.
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

            # Detectar menciones de bajas del local
            if l_q.lower() in texto_completo:
                if any(p in texto_completo for p in PALABRAS_BAJA_LOCAL):
                    penalty_local = 0.95

            # Detectar menciones de bajas del visitante
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
        r = await asyncio.to_thread(requests.get, url, headers=headers)
        sha = r.json()['sha'] if r.status_code == 200 else None
        
        if historial_completo is None:
            if r.status_code == 200:
                historial = json.loads(base64.b64decode(r.json()['content']).decode('utf-8'))
            else:
                historial = []
            if nuevo_registro: historial.append(nuevo_registro)
        else:
            historial = historial_completo

        nuevo_contenido = base64.b64encode(json.dumps(historial, indent=4, ensure_ascii=False).encode('utf-8')).decode('utf-8')
        payload = {
            "message": "🤖 Actualización de Historial",
            "content": nuevo_contenido,
            "sha": sha
        }
        await asyncio.to_thread(requests.put, url, headers=headers, json=payload)
    except Exception as e:
        logging.error(f"Error GitHub: {e}")

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
    if not ODDS_API_KEY: return 1.85, 3.50, 4.00, False
    try:
        url = "https://api.the-odds-api.com/v4/sports/soccer_spain_la_liga/odds/"
        params = {'apiKey': ODDS_API_KEY, 'regions': 'eu', 'markets': 'h2h'}
        r = await asyncio.to_thread(requests.get, url, params=params, timeout=10)
        if r.status_code == 200:
            for match in r.json():
                home = match['home_team'].lower()
                query = equipo_l.lower()
                if query in home or home in query:
                    odds = match['bookmakers'][0]['markets'][0]['outcomes']
                    ol = next(o['price'] for o in odds if o['name'] == match['home_team'])
                    ov = next(o['price'] for o in odds if o['name'] == match['away_team'])
                    oe = next(o['price'] for o in odds if o['name'] == 'Draw')
                    return ol, oe, ov, True
    except: pass
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

# --- MEJORA 3: H2H filtrado por sede ---
async def obtener_h2h_directo(id_l, id_v):
    """
    Devuelve el H2H filtrado por sede:
    - Solo cuenta victorias del LOCAL cuando id_l era HOME_TEAM
    - Solo cuenta victorias del VISITANTE cuando id_v era HOME_TEAM
    Esto elimina el sesgo de contar victorias fuera de casa como si fueran en casa.
    """
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
                    # Solo contamos si id_l fue realmente local en ese partido
                    if home_id == id_l:
                        if w == 'HOME_TEAM': l += 1
                        elif w == 'AWAY_TEAM': v += 1
                        else: e += 1
                    else:
                        # id_l fue visitante: invertimos la perspectiva
                        if w == 'HOME_TEAM': v += 1
                        elif w == 'AWAY_TEAM': l += 1
                        else: e += 1
                total = l + v + e
                return f"Local {l} | Visitante {v} | Empates {e} (sede-ajustado)", True, l, v, total
        return "H2H: Sin datos directos.", False, 0, 0, 0
    except:
        return "H2H: Error API.", False, 0, 0, 0


def calcular_factor_h2h(home_wins, away_wins, total_partidos):
    """
    Convierte el historial H2H en un factor de ajuste para las lambdas de Poisson.
    Peso máximo ±8%, solo actúa si un equipo ganó >60% de los H2H.
    """
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


# --- MEJORA 1: Forma reciente (últimos 5 partidos) ---
async def obtener_forma_reciente(team_id):
    """
    Consulta los últimos 5 partidos FINISHED del equipo.
    Devuelve (factor_lh, factor_la, texto) donde:
    - 5W → factor_ataque=+10%, factor_defensa=-10%
    - 5L → factor_ataque=-10%, factor_defensa=+10%
    - Escala proporcional entre ambos extremos.
    El factor se aplica sobre las lambdas del equipo correspondiente.
    """
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
            # derrota: 0 puntos

        # Escala: 0pts (forma 0.0) → 15pts (forma 1.0)
        # Zona neutra 5-10pts: sin ajuste
        MAX_AJUSTE = 0.10
        forma_norm = puntos / 15.0  # 0.0 a 1.0

        if forma_norm > 0.67:  # >10 pts: buen momento
            intensidad = (forma_norm - 0.67) / 0.33
            ajuste = MAX_AJUSTE * intensidad
            factor_ataque = 1.0 + ajuste
            factor_defensa = 1.0 - ajuste
            simbolo = "🔥"
        elif forma_norm < 0.33:  # <5 pts: mal momento
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


# --- MEJORA 2: Posición en tabla ---
async def obtener_posiciones_tabla():
    """
    Devuelve dict {team_id: {pos, puntos}} para calcular diferencia de posición.
    Usa el endpoint de standings de Football-Data.
    """
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
    """
    Ajusta lambdas según diferencia de posición en tabla.
    - Diferencia >6 posiciones a favor del local: lh sube hasta 6%
    - Diferencia >6 posiciones a favor del visitante: la sube hasta 6%
    - Peso conservador: máx ±6% para no dominar sobre Poisson.
    """
    MAX_AJUSTE = 0.06
    diff_pos = pos_visita - pos_local  # positivo = local mejor posicionado

    if abs(diff_pos) < 6:
        return 1.0, 1.0, f"Tabla ⚖️ Diferencia leve ({pos_local}° vs {pos_visita}°, {pts_local}pts vs {pts_visita}pts)"

    intensidad = min((abs(diff_pos) - 6) / 14, 1.0)  # escala 6-20 diff → 0-1
    ajuste = MAX_AJUSTE * intensidad

    if diff_pos > 0:  # local mejor posicionado
        factor_lh = 1.0 + ajuste
        factor_la = 1.0 - ajuste
        texto = f"Tabla 📈 Local superior ({pos_local}° vs {pos_visita}°, +{ajuste*100:.1f}% lh)"
    else:  # visitante mejor posicionado
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

# --- Comando Principal: Pronóstico V9 ---
@bot.message_handler(commands=['pronostico', 'valor'])
async def handle_pronostico(message):
    if not SISTEMA_IA["estratega"]["nodo"]:
        await bot.reply_to(message, "🚨 Configura los nodos con `/config`."); return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or " vs " not in parts[1]:
        await bot.reply_to(message, "⚠️ `/pronostico Local vs Visitante`."); return

    l_q, v_q = [t.strip() for t in parts[1].split(" vs ")]
    msg_espera = await bot.reply_to(message, "📡 Ejecutando Análisis V9 (Poisson + DC + H2H-Sede + Forma + Tabla + Odds)...")

    full_data, check_json = await obtener_modelo()
    if not full_data:
        await bot.edit_message_text("❌ Error al cargar el JSON del servidor.", message.chat.id, msg_espera.message_id); return

    # Tareas en paralelo: odds, noticias, calibración
    task_odds = obtener_datos_mercado(l_q)
    task_news = obtener_contexto_real(l_q, v_q)
    task_calib = obtener_factor_calibracion()
    c_l, c_e, c_v, check_odds = await task_odds
    contexto_noticias, penalty_local, penalty_visita = await task_news
    factor_calibracion = await task_calib

    liga = next(iter(full_data))
    m_l = next((t for t in full_data[liga]['teams'] if t.lower() in l_q.lower() or l_q.lower() in t.lower()), None)
    m_v = next((t for t in full_data[liga]['teams'] if t.lower() in v_q.lower() or v_q.lower() in t.lower()), None)
    
    if not m_l or not m_v:
        await bot.edit_message_text("❌ Equipo no encontrado en el JSON.", message.chat.id, msg_espera.message_id); return

    l_s, v_s = full_data[liga]['teams'][m_l], full_data[liga]['teams'][m_v]
    id_l = l_s.get("id_api")
    id_v = v_s.get("id_api")

    # Todas las consultas externas en paralelo
    h2h_task = obtener_h2h_directo(id_l, id_v)
    forma_local_task = obtener_forma_reciente(id_l)
    forma_visita_task = obtener_forma_reciente(id_v)
    tabla_task = obtener_posiciones_tabla()

    h2h, check_h2h, home_wins, away_wins, total_h2h = await h2h_task
    forma_local_atk, forma_local_def, forma_local_txt = await forma_local_task
    forma_visita_atk, forma_visita_def, forma_visita_txt = await forma_visita_task
    tabla = await tabla_task

    # --- PASO 1: Lambdas base desde Poisson ---
    avg = full_data[liga]['averages']
    lh_base = l_s['att_h'] * v_s['def_a'] * avg['league_home']
    la_base = v_s['att_a'] * l_s['def_h'] * avg['league_away']

    # --- PASO 2: Ajuste H2H (sede-corregido) ---
    factor_lh_h2h, factor_la_h2h, h2h_texto = calcular_factor_h2h(home_wins, away_wins, total_h2h)

    # --- PASO 3: Ajuste forma reciente ---
    # Forma local afecta lh (ataque) y la resistencia defensiva que enfrenta el visitante
    # Forma visita afecta la (ataque visitante) y la resistencia defensiva que enfrenta el local
    factor_lh_forma = forma_local_atk      # local en buen momento ataca mejor
    factor_la_forma = forma_visita_atk     # visita en buen momento ataca mejor

    # --- PASO 4: Ajuste tabla ---
    factor_lh_tabla, factor_la_tabla, tabla_texto = 1.0, 1.0, "Tabla: Sin datos"
    if tabla and id_l in tabla and id_v in tabla:
        t_l = tabla[id_l]
        t_v = tabla[id_v]
        factor_lh_tabla, factor_la_tabla, tabla_texto = calcular_factor_tabla(
            t_l['pos'], t_v['pos'], t_l['puntos'], t_v['puntos']
        )

    # --- PASO 5: Combinar todos los factores sobre las lambdas ---
    lh = lh_base * factor_lh_h2h * factor_lh_forma * factor_lh_tabla * penalty_local
    la = la_base * factor_la_h2h * factor_la_forma * factor_la_tabla * penalty_visita

    # --- PASO 6: Probabilidad Poisson 7x7 + Dixon-Coles ---
    prob_poisson = 0
    for x in range(7):
        for y in range(7):
            p = poisson.pmf(x, lh) * poisson.pmf(y, la) * dixon_coles_tau(x, y, lh, la)
            if x > y:
                prob_poisson += p

    # --- PASO 7: Calibración histórica ---
    prob_poisson_calibrado = prob_poisson * factor_calibracion

    # --- PASO 8: Mezcla final Poisson(85%) + Mercado(15%) ---
    prob_market = 1 / c_l
    p_win = (prob_poisson_calibrado * 0.85) + (prob_market * 0.15)
    p_percent = p_win * 100

    # --- PASO 9: Over/Under como confirmación del stake ---
    ou_factor, ou_texto = await obtener_confirmacion_ou(l_q, lh, la)

    # --- PASO 10: Edge y Kelly ---
    edge_real = p_win - prob_market
    margen_error = 0.01
    edge_ajustado = edge_real - margen_error

    # Filtro cuotas trampa
    if 1.90 <= c_l <= 2.20 and edge_ajustado < 0.02:
        edge_ajustado = -0.001

    # MEJORA 6: Cuota del empate como filtro adicional
    # Si c_e < 3.0, el mercado dice partido muy parejo → reducir edge un 20%
    if c_e < 3.0 and edge_ajustado > 0:
        edge_ajustado *= 0.80
        empate_aviso = f"⚠️ Cuota empate baja ({c_e:.2f}) → edge reducido 20%"
    else:
        empate_aviso = f"Cuota empate: {c_e:.2f} ✅"

    if edge_ajustado <= 0:
        nivel, stake, pick_final = "NO BET 🚫", 0, "No Bet"
        ou_factor = 1.0
    else:
        kelly_full = edge_ajustado / (c_l - 1)
        kelly_fraccionado = kelly_full * 0.25
        stake_base = round(kelly_fraccionado * 100, 2)
        stake = round(stake_base * ou_factor, 2)
        stake = max(0.25, min(stake, 3.0))
        pick_final = m_l
        if stake < 0.75:
            nivel = "BRONCE 🥉"
        elif stake < 1.25:
            nivel = "PLATA 🥈"
        elif stake < 2.0:
            nivel = "ORO 🥇"
        else:
            nivel = "DIAMANTE 💎"

    # --- Guardado en Historial ---
    fecha_hoy = (datetime.now(timezone.utc) + timedelta(hours=OFFSET_JUAREZ)).strftime('%Y-%m-%d %H:%M')
    async def task_github():
        await guardar_en_github(nuevo_registro={
            "fecha": fecha_hoy,
            "partido": f"{m_l} vs {m_v}",
            "pick": pick_final,
            "poisson": f"{p_percent:.1f}%",
            "cuota": c_l,
            "edge": f"{edge_ajustado*100:.1f}%",
            "stake": f"{stake}%",
            "nivel": nivel,
            "status": "⏳ PENDIENTE"
        })

    clave_partido = f"{m_l}_vs_{m_v}_{fecha_hoy[:10]}"
    ahora = datetime.now(timezone.utc)
    if clave_partido in COOLDOWN and (ahora - COOLDOWN[clave_partido]).total_seconds() < COOLDOWN_MINUTOS * 60:
        pass
    else:
        COOLDOWN[clave_partido] = ahora
        asyncio.create_task(task_github())

    # --- Construir textos resumen de ajustes ---
    calib_txt = f"{factor_calibracion:.2f}" if factor_calibracion != 1.0 else "1.00 (sin datos)"
    h2h_ajuste_txt = f"+{(factor_lh_h2h-1)*100:.1f}%lh" if factor_lh_h2h != 1.0 else (f"+{(factor_la_h2h-1)*100:.1f}%la" if factor_la_h2h != 1.0 else "sin ajuste")
    serper_txt = ""
    if penalty_local < 1.0: serper_txt += f" ⚠️ Bajas local (-{(1-penalty_local)*100:.0f}%lh)"
    if penalty_visita < 1.0: serper_txt += f" ⚠️ Bajas visita (-{(1-penalty_visita)*100:.0f}%la)"
    if not serper_txt: serper_txt = " Sin bajas detectadas"

    header = (
        f"<b>🛠 REPORTE V9:</b> {'✅' if check_odds else '❌'} Mercado | "
        f"{'✅' if check_json else '❌'} Poisson 7x7+DC ({p_percent:.1f}%) | "
        f"{'✅' if check_h2h else '❌'} H2H\n"
        f"<b>⚽ Lambdas:</b> λH={lh:.2f} (base {lh_base:.2f}) | λA={la:.2f} (base {la_base:.2f})\n"
        f"<b>🔄 H2H (sede):</b> {h2h_texto} → {h2h_ajuste_txt}\n"
        f"<b>📅 Forma:</b> {forma_local_txt} | {forma_visita_txt}\n"
        f"<b>🏆 Tabla:</b> {tabla_texto}\n"
        f"<b>📰 Serper:</b>{serper_txt}\n"
        f"<b>📊 Calibración:</b> ×{calib_txt} | {ou_texto}\n"
        f"<b>🎲 Empate:</b> {empate_aviso}\n"
        f"{'—'*20}\n"
    )

    # --- Prompt IA con todos los datos ---
    prompt_e = f"""
Eres analista profesional de fútbol. Partido: {m_l} vs {m_v}

DATOS MATEMÁTICOS:
- Prob. victoria local (modelo): {p_percent:.1f}%
- Cuota mercado local: {c_l} (implica {prob_market*100:.1f}%)
- Edge ajustado: {edge_ajustado*100:.2f}%
- H2H últimos partidos (sede-corregido): {h2h} → ajuste: {h2h_ajuste_txt}
- Forma reciente local: {forma_local_txt}
- Forma reciente visita: {forma_visita_txt}
- Posición en tabla: {tabla_texto}
- Bajas detectadas (Serper):{serper_txt}
- Cuota empate: {c_e:.2f} ({empate_aviso})
- Señal O/U: {ou_texto}
- Lambda local ajustada: {lh:.2f} goles esperados
- Lambda visita ajustada: {la:.2f} goles esperados

INSTRUCCIONES:
1. Si edge <= 0, di NO BET y explica brevemente por qué no hay valor.
2. Si edge > 0, explica en máximo 120 palabras por qué hay valor combinando Poisson, forma reciente, H2H, tabla y mercado.
3. Menciona si la forma reciente y la tabla refuerzan o contradicen el pronóstico estadístico.
4. Si hay bajas detectadas, pondera su impacto.

🎯 PICK: {pick_final}
📈 NIVEL: {nivel}
💰 STAKE Kelly: {stake}% del bankroll
"""

    analisis_raw = await ejecutar_ia("estratega", prompt_e)
    analisis = html.escape(analisis_raw)
    footer = f"\n\n{'—'*20}\n🛰 <b>ESTRATEGA:</b> <code>{SISTEMA_IA['estratega']['api']}</code>"

    if SISTEMA_IA["auditor"]["nodo"]:
        prompt_a = (
            f"ERES AUDITOR. Valida este análisis: '{analisis_raw}'\n"
            f"NOTICIAS RECIENTES:\n{contexto_noticias}\n"
            f"H2H (sede-corregido): {h2h}\n"
            f"Forma local: {forma_local_txt}\n"
            f"Forma visita: {forma_visita_txt}\n"
            f"Tabla: {tabla_texto}\n"
            f"¿Hay alguna contradicción entre el análisis y los datos? Resumen muy breve."
        )
        auditoria_raw = await ejecutar_ia("auditor", prompt_a)
        footer += f"\n🛡 <b>AUDITOR:</b> <code>{SISTEMA_IA['auditor']['api']}</code>"
        final = f"{header}{analisis}\n\n{html.escape(auditoria_raw)}{footer}"
    else:
        final = f"{header}{analisis}{footer}"

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
        historial_raw = r_hist.json()
        data_api = await api_football_call("matches?status=FINISHED")
        if not data_api:
            await bot.edit_message_text("❌ Sin resultados nuevos.", message.chat.id, msg_espera.message_id); return
        count = 0
        for item in historial_raw:
            if item.get("status") == "⏳ PENDIENTE":
                for m in data_api['matches']:
                    h_api = m['homeTeam']['name'].lower()
                    a_api = m['awayTeam']['name'].lower()
                    if h_api in item['partido'].lower() and a_api in item['partido'].lower():
                        winner = m['score']['winner']
                        item['status'] = evaluar_resultado(item['pick'], item['partido'], h_api, a_api, winner)
                        item['marcador_real'] = f"{m['score']['fullTime']['home']}-{m['score']['fullTime']['away']}"
                        count += 1
        if count > 0:
            await guardar_en_github(historial_completo=historial_raw)
            await bot.edit_message_text(f"✅ {count} partidos validados.", message.chat.id, msg_espera.message_id)
        else:
            await bot.edit_message_text("ℹ️ Nada que actualizar.", message.chat.id, msg_espera.message_id)
    except:
        await bot.edit_message_text("❌ Fallo en validación.", message.chat.id, msg_espera.message_id)

@bot.message_handler(commands=['partidos'])
async def cmd_partidos(message):
    data = await api_football_call("matches?status=SCHEDULED")
    if not data: return
    txt = "📅 <b>PARTIDOS (HORA JUÁREZ)</b>\n\n"
    for m in data['matches'][:10]:
        dt = datetime.strptime(m['utcDate'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(hours=OFFSET_JUAREZ)
        txt += f"🕒 <code>{dt.strftime('%H:%M')}</code> | <code>{dt.strftime('%d/%m')}</code>\n🏠 <b>{m['homeTeam']['shortName']}</b> vs 🚩 <b>{m['awayTeam']['shortName']}</b>\n{'—'*15}\n"
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
        "🤖 <b>SISTEMA V9.0 PRO</b>\n\n"
        "📈 <b>ANÁLISIS:</b>\n"
        "• <code>/pronostico Local vs Visitante</code>: Poisson 7x7 + Dixon-Coles + H2H-Sede + Forma + Tabla + Odds + Kelly.\n"
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
