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

# ===========================================================================
# MEJORA 2: Dixon-Coles aplicado en el bot (antes solo en test_prediccion.py)
# Corrige la sobreestimación de resultados con pocos goles (0-0, 1-0, 0-1, 1-1)
# ===========================================================================
def ajuste_dixon_coles(x, y, lh, la, rho=-0.15):
    """Ajuste de correlación para marcadores bajos."""
    if x == 0 and y == 0: return 1 - (lh * la * rho)
    if x == 0 and y == 1: return 1 + (lh * rho)
    if x == 1 and y == 0: return 1 + (la * rho)
    if x == 1 and y == 1: return 1 - rho
    return 1.0

# ===========================================================================
# MEJORA 8: Forma reciente desde la API (últimos 5 partidos)
# Multiplicador separado al time-decay: captura rachas calientes/frías
# ===========================================================================
async def obtener_forma_reciente(team_id):
    """
    Obtiene los últimos 5 partidos del equipo y calcula un multiplicador de forma.
    Retorna (multiplicador_ataque, multiplicador_defensa, texto_resumen)
    """
    if not team_id or not FOOTBALL_DATA_KEY:
        return 1.0, 1.0, "Forma: N/D"

    headers = {'X-Auth-Token': FOOTBALL_DATA_KEY}
    try:
        url = f"https://api.football-data.org/v4/teams/{team_id}/matches?status=FINISHED&limit=5"
        r = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
        if r.status_code != 200:
            return 1.0, 1.0, "Forma: N/D"

        matches = r.json().get('matches', [])
        if not matches:
            return 1.0, 1.0, "Forma: Sin partidos"

        goles_favor = []
        goles_contra = []
        resultados = []

        for m in matches[-5:]:
            is_home = m['homeTeam']['id'] == team_id
            gf = m['score']['fullTime']['home'] if is_home else m['score']['fullTime']['away']
            gc = m['score']['fullTime']['away'] if is_home else m['score']['fullTime']['home']
            winner = m['score']['winner']

            goles_favor.append(gf)
            goles_contra.append(gc)

            if winner == 'HOME_TEAM' and is_home: resultados.append('W')
            elif winner == 'AWAY_TEAM' and not is_home: resultados.append('W')
            elif winner == 'DRAW': resultados.append('D')
            else: resultados.append('L')

        avg_gf = sum(goles_favor) / len(goles_favor) if goles_favor else 1.0
        avg_gc = sum(goles_contra) / len(goles_contra) if goles_contra else 1.0

        # Multiplicador de ataque: si el equipo mete más de 1.5 goles/partido en racha → bonus
        # Si mete menos de 0.8 → penalización
        mult_ataque = 1.0
        if avg_gf >= 2.0:   mult_ataque = 1.08
        elif avg_gf >= 1.5: mult_ataque = 1.04
        elif avg_gf <= 0.8: mult_ataque = 0.93
        elif avg_gf <= 0.5: mult_ataque = 0.87

        # Multiplicador defensivo inverso: si concede poco → bonus defensivo
        mult_defensa = 1.0
        if avg_gc <= 0.6:   mult_defensa = 0.90   # defiende muy bien → rival marca menos
        elif avg_gc <= 1.0: mult_defensa = 0.95
        elif avg_gc >= 2.0: mult_defensa = 1.08   # defiende mal → rival marca más
        elif avg_gc >= 1.5: mult_defensa = 1.04

        forma_str = "".join(resultados)
        wins = resultados.count('W')
        return mult_ataque, mult_defensa, f"Forma: {forma_str} ({wins}/5 victorias, {avg_gf:.1f} GF, {avg_gc:.1f} GC)"

    except Exception as e:
        logging.error(f"Error forma reciente: {e}")
        return 1.0, 1.0, "Forma: Error"

# ===========================================================================
# MEJORA 9: Serper con contexto de lesiones y bajas (antes solo alineaciones)
# ===========================================================================
async def obtener_contexto_real(l_q, v_q):
    if not SERPER_KEY:
        return "No hay API Key de Serper configurada."

    url = "https://google.serper.dev/search"
    # MEJORA: query expandido para capturar lesiones, bajas y alineaciones
    query = f'(site:jornadaperfecta.com OR site:futbolfantasy.com OR site:besoccer.com) "{l_q}" "{v_q}" lesiones bajas alineación'

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
        for item in res[:3]:
            contexto += f"- {item['title']}: {item['snippet']}\n"
        return contexto if contexto else "No se encontraron noticias recientes."
    except Exception as e:
        logging.error(f"Error Serper: {e}")
        return "Error consultando noticias de última hora."

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

async def api_football_call(endpoint):
    headers = {'X-Auth-Token': FOOTBALL_DATA_KEY}
    try:
        r = await asyncio.to_thread(requests.get, f"https://api.football-data.org/v4/competitions/PD/{endpoint}", headers=headers, timeout=10)
        return r.json() if r.status_code == 200 else None
    except: return None

async def obtener_h2h_directo(id_l, id_v):
    if not id_l or not id_v:
        return "H2H: Sin IDs válidos.", False, 0, 0

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
                    if w == 'HOME_TEAM': l += 1
                    elif w == 'AWAY_TEAM': v += 1
                    else: e += 1
                return f"Local {l} | Visitante {v} | Empates {e}", True, l, v
        return "H2H: Sin datos directos.", False, 0, 0
    except:
        return "H2H: Error API.", False, 0, 0

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

# ===========================================================================
# MEJORA 9 (adicional): Filtro de motivación por posición en tabla
# Detecta si algún equipo está en zona "muerta" (ya campeón o ya descendido)
# ===========================================================================
async def evaluar_motivacion(nombre_local, nombre_visitante):
    """
    Consulta la tabla y detecta si algún equipo tiene poca motivación
    (campeón asegurado, clasificado a Champions ya, o descendido matemáticamente).
    Retorna un texto de advertencia o cadena vacía.
    """
    data = await api_football_call("standings")
    if not data:
        return ""

    try:
        tabla = data['standings'][0]['table']
        total_equipos = len(tabla)
        advertencias = []

        for entry in tabla:
            team_name = entry['team']['name']
            pos = entry['position']
            puntos = entry['points']
            jugados = entry['playedGames']
            restantes = 38 - jugados  # LaLiga son 38 jornadas

            nombre_lower = team_name.lower()
            es_local = nombre_local.lower() in nombre_lower or nombre_lower in nombre_local.lower()
            es_visita = nombre_visitante.lower() in nombre_lower or nombre_lower in nombre_visitante.lower()

            if not es_local and not es_visita:
                continue

            rol = "Local" if es_local else "Visitante"

            # Campeón asegurado: ventaja > puntos posibles del 2do
            if pos == 1 and puntos - tabla[1]['points'] > restantes * 3:
                advertencias.append(f"⚠️ {rol} ({team_name}) podría ya ser campeón asegurado.")

            # Zona Champions asegurada (top 4) con amplio margen
            if pos <= 4 and tabla[4]['points'] + restantes * 3 < puntos:
                advertencias.append(f"ℹ️ {rol} ({team_name}) tiene Champions asegurada.")

            # Descenso matemático (últimos 3)
            if pos >= total_equipos - 2:
                if tabla[total_equipos - 4]['points'] > puntos + restantes * 3:
                    advertencias.append(f"⚠️ {rol} ({team_name}) podría estar descendido matemáticamente.")

        return "\n".join(advertencias) if advertencias else ""

    except Exception as e:
        logging.error(f"Error motivación: {e}")
        return ""

# --- Comando Principal: Pronóstico V6 con todas las mejoras ---
@bot.message_handler(commands=['pronostico', 'valor'])
async def handle_pronostico(message):
    if not SISTEMA_IA["estratega"]["nodo"]:
        await bot.reply_to(message, "🚨 Configura los nodos con `/config`."); return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or " vs " not in parts[1]:
        await bot.reply_to(message, "⚠️ `/pronostico Local vs Visitante`."); return

    l_q, v_q = [t.strip() for t in parts[1].split(" vs ")]
    msg_espera = await bot.reply_to(message, "📡 Ejecutando Análisis V6...")

    try:
        raw_json = await asyncio.to_thread(requests.get, URL_JSON, timeout=10)
        full_data = raw_json.json()
        check_json = raw_json.status_code == 200
    except:
        await bot.edit_message_text("❌ Error al cargar el JSON del servidor.", message.chat.id, msg_espera.message_id); return

    # Lanzar todas las tareas IO en paralelo
    task_odds = obtener_datos_mercado(l_q)
    task_news = obtener_contexto_real(l_q, v_q)
    c_l, c_e, c_v, check_odds = await task_odds
    contexto_noticias = await task_news

    liga = next(iter(full_data))
    m_l = next((t for t in full_data[liga]['teams'] if t.lower() in l_q.lower() or l_q.lower() in t.lower()), None)
    m_v = next((t for t in full_data[liga]['teams'] if t.lower() in v_q.lower() or v_q.lower() in t.lower()), None)

    if not m_l or not m_v:
        await bot.edit_message_text("❌ Equipo no encontrado en el JSON.", message.chat.id, msg_espera.message_id); return

    l_s, v_s = full_data[liga]['teams'][m_l], full_data[liga]['teams'][m_v]
    id_local = l_s.get("id_api")
    id_visita = v_s.get("id_api")

    # Obtener H2H, forma reciente y motivación en paralelo
    h2h_task = obtener_h2h_directo(id_local, id_visita)
    forma_l_task = obtener_forma_reciente(id_local)
    forma_v_task = obtener_forma_reciente(id_visita)
    motivacion_task = evaluar_motivacion(m_l, m_v)

    h2h, check_h2h, home_wins, away_wins = await h2h_task
    mult_atk_l, mult_def_l, forma_txt_l = await forma_l_task
    mult_atk_v, mult_def_v, forma_txt_v = await forma_v_task
    advertencia_motivacion = await motivacion_task

    avg = full_data[liga]['averages']

    # ===========================================================================
    # MEJORA 1: Calcular lambdas base con Poisson
    # MEJORA 8: Aplicar multiplicadores de forma reciente a las lambdas
    # ===========================================================================
    lh_base = l_s['att_h'] * v_s['def_a'] * avg['league_home']
    la_base = v_s['att_a'] * l_s['def_h'] * avg['league_away']

    # Forma reciente ajusta las lambdas (separado del time-decay del trainer)
    lh = lh_base * mult_atk_l * mult_def_v
    la = la_base * mult_atk_v * mult_def_l

    # ===========================================================================
    # MEJORA 4: H2H ajusta lambdas matemáticamente (antes solo iba al prompt)
    # ===========================================================================
    if home_wins >= 4:
        lh *= 1.03
    elif away_wins >= 4:
        la *= 1.03
    elif home_wins == 0 and away_wins >= 3:
        la *= 1.02

    # ===========================================================================
    # MEJORA 1: Calcular las 3 probabilidades (antes solo se calculaba victoria local)
    # MEJORA 2: Aplicar Dixon-Coles en cada celda de la matriz
    # MEJORA 6: Matriz 8x8 (antes 6x6, truncaba goles altos de Barça/Madrid)
    # ===========================================================================
    ph, pd, pa = 0.0, 0.0, 0.0
    over25 = 0.0
    scores = []

    for x in range(8):
        for y in range(8):
            dc = ajuste_dixon_coles(x, y, lh, la)
            p = poisson.pmf(x, lh) * poisson.pmf(y, la) * dc

            if x > y:   ph += p
            elif x == y: pd += p
            else:        pa += p

            if (x + y) > 2.5: over25 += p
            if x <= 5 and y <= 5:
                scores.append((f"{x}-{y}", p))

    scores.sort(key=lambda s: s[1], reverse=True)

    # ===========================================================================
    # MEJORA 1: Elegir el resultado con mayor edge (no solo apostar al local)
    # MEJORA 5: Peso del mercado aumentado a 20% cuando las cuotas son reales
    # ===========================================================================
    peso_mercado = 0.20 if check_odds else 0.10
    peso_poisson = 1.0 - peso_mercado

    # Probabilidades implícitas del mercado (sin margen de la casa)
    prob_market_l = 1 / c_l
    prob_market_e = 1 / c_e
    prob_market_v = 1 / c_v

    # Probabilidades suavizadas para cada resultado
    p_local   = (ph * peso_poisson) + (prob_market_l * peso_mercado)
    p_empate  = (pd * peso_poisson) + (prob_market_e * peso_mercado)
    p_visita  = (pa * peso_poisson) + (prob_market_v * peso_mercado)

    # Edge para cada resultado
    edge_local  = p_local  - prob_market_l
    edge_empate = p_empate - prob_market_e
    edge_visita = p_visita - prob_market_v

    margen_error = 0.01

    # Seleccionar el resultado con mayor edge ajustado
    candidatos = [
        (edge_local  - margen_error, p_local  * 100, c_l, m_l,     "local"),
        (edge_empate - margen_error, p_empate * 100, c_e, "Empate", "empate"),
        (edge_visita - margen_error, p_visita * 100, c_v, m_v,      "visita"),
    ]
    candidatos.sort(key=lambda x: x[0], reverse=True)
    edge_ajustado, p_percent, cuota_pick, nombre_pick, tipo_pick = candidatos[0]

    # Filtro cuotas trampa para el resultado elegido
    if 1.90 <= cuota_pick <= 2.20 and edge_ajustado < 0.02:
        edge_ajustado = -0.001

    # Niveles de stake
    if edge_ajustado <= 0:
        nivel, stake, pick_final = "NO BET 🚫", 0, "No Bet"
    elif edge_ajustado < 0.04:
        nivel, stake, pick_final = "BRONCE 🥉", 0.50, nombre_pick
    elif edge_ajustado < 0.07:
        nivel, stake, pick_final = "PLATA 🥈", 1.00, nombre_pick
    elif edge_ajustado < 0.10:
        nivel, stake, pick_final = "ORO 🥇", 1.50, nombre_pick
    else:
        nivel, stake, pick_final = "DIAMANTE 💎", 2.50, nombre_pick

    # Guardar en historial
    fecha_hoy = (datetime.now(timezone.utc) + timedelta(hours=OFFSET_JUAREZ)).strftime('%Y-%m-%d %H:%M')

    async def task_github():
        await guardar_en_github(nuevo_registro={
            "fecha": fecha_hoy,
            "partido": f"{m_l} vs {m_v}",
            "pick": pick_final,
            "poisson": f"{p_percent:.1f}%",
            "cuota": cuota_pick,
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

    # ===========================================================================
    # MEJORA 7: Prompt del Estratega ahora recibe cuotas de los 3 resultados,
    # forma reciente, advertencia de motivación y top 3 marcadores
    # ===========================================================================
    top3_scores = ", ".join([f"{s[0]}({s[1]:.1%})" for s in scores[:3]])

    header = (f"<b>🛠 REPORTE V6:</b> {'✅' if check_odds else '❌'} Mercado | "
              f"{'✅' if check_json else '❌'} Poisson | "
              f"{'✅' if check_h2h else '❌'} H2H\n"
              f"————————————————————\n")

    # Bloque de forma reciente para el mensaje
    forma_bloque = (f"📊 <b>FORMA RECIENTE:</b>\n"
                    f"🏠 {m_l}: {forma_txt_l}\n"
                    f"🚩 {m_v}: {forma_txt_v}\n")

    prompt_e = f"""
Eres analista profesional de apuestas deportivas.
Partido: {m_l} vs {m_v}

DATOS ESTADÍSTICOS:
- Lambda Local (goles esperados): {lh:.2f} | Lambda Visita: {la:.2f}
- Prob. Local: {p_local*100:.1f}% (cuota {c_l}) | Edge: {edge_local*100:.2f}%
- Prob. Empate: {p_empate*100:.1f}% (cuota {c_e}) | Edge: {edge_empate*100:.2f}%
- Prob. Visita: {p_visita*100:.1f}% (cuota {c_v}) | Edge: {edge_visita*100:.2f}%
- Over 2.5: {over25*100:.1f}%
- Top 3 marcadores: {top3_scores}
- H2H (últimos 5): {h2h}
- {forma_txt_l}
- {forma_txt_v}
{f"- ADVERTENCIA MOTIVACIÓN: {advertencia_motivacion}" if advertencia_motivacion else ""}

INSTRUCCIONES:
1. Si edge <= 0, justifica brevemente por qué NO hay valor.
2. Si edge > 0, explica por qué hay valor considerando los 3 resultados posibles.
3. Menciona si la forma reciente confirma o contradice la predicción.
4. Máximo 120 palabras.

🎯 PICK SELECCIONADO: {pick_final}
📈 NIVEL: {nivel}
💰 STAKE: {stake}%
"""

    analisis_raw = await ejecutar_ia("estratega", prompt_e)
    analisis = html.escape(analisis_raw)
    footer = f"\n\n{'—'*20}\n🛰 <b>ESTRATEGA:</b> <code>{SISTEMA_IA['estratega']['api']}</code>"

    if SISTEMA_IA["auditor"]["nodo"]:
        prompt_a = (
            f"ERES AUDITOR. Valida brevemente este análisis:\n'{analisis_raw}'\n\n"
            f"CONTEXTO NOTICIAS (lesiones/bajas/alineaciones):\n{contexto_noticias}\n"
            f"{f'ADVERTENCIA: {advertencia_motivacion}' if advertencia_motivacion else ''}\n"
            f"Resumen muy breve, máximo 60 palabras. Señala solo si hay algo que contradiga el pick."
        )
        auditoria_raw = await ejecutar_ia("auditor", prompt_a)
        footer += f"\n🛡 <b>AUDITOR:</b> <code>{SISTEMA_IA['auditor']['api']}</code>"

        # Advertencia de motivación visible en el mensaje si existe
        motivacion_txt = f"\n\n⚠️ <b>MOTIVACIÓN:</b> {html.escape(advertencia_motivacion)}" if advertencia_motivacion else ""
        final = f"{header}{forma_bloque}\n{analisis}\n\n{html.escape(auditoria_raw)}{motivacion_txt}{footer}"
    else:
        motivacion_txt = f"\n\n⚠️ <b>MOTIVACIÓN:</b> {html.escape(advertencia_motivacion)}" if advertencia_motivacion else ""
        final = f"{header}{forma_bloque}\n{analisis}{motivacion_txt}{footer}"

    await bot.edit_message_text(final, message.chat.id, msg_espera.message_id, parse_mode='HTML')

# --- Comandos Adicionales ---

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
    await bot.edit_message_text("🚀 <b>SISTEMA V6 LISTO</b>", call.message.chat.id, call.message.message_id, parse_mode='HTML')

@bot.message_handler(commands=['help'])
async def cmd_help(message):
    help_text = (
        "🤖 <b>SISTEMA V6 PRO</b>\n\n"
        "📈 <b>ANÁLISIS:</b>\n"
        "• <code>/pronostico Local vs Visitante</code>: Análisis completo.\n"
        "• <code>/historial</code>: Últimos pronósticos.\n"
        "• <code>/validar</code>: Sincroniza resultados GitHub.\n"
        "• <code>/config</code>: Configura IA.\n\n"
        "🛡 <b>ROLES:</b>\n"
        "• <b>[EST]:</b> Estratega (Análisis matemático y Kelly).\n"
        "• <b>[AUD]:</b> Auditor (Redacción y verificación lógica).\n\n"
        "⚽ <b>INFORMACIÓN:</b>\n"
        "• <code>/partidos</code>: Próximos encuentros.\n"
        "• <code>/tabla</code>: Posiciones liga.\n"
        "• <code>/equipos</code>: Lista equipos JSON.\n\n"
        "🔧 <b>MEJORAS V6:</b>\n"
        "• Evalúa los 3 resultados (local/empate/visita)\n"
        "• Dixon-Coles activo en predicción\n"
        "• Forma reciente ajusta lambdas\n"
        "• H2H ajusta lambdas matemáticamente\n"
        "• Filtro de motivación por posición en tabla\n"
        "• Matriz 8×8 para goles altos\n"
    )
    await bot.reply_to(message, help_text, parse_mode='HTML')

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.polling(non_stop=True)

if __name__ == "__main__":
    asyncio.run(main())
