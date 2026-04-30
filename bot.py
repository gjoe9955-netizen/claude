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

# --- Función de Búsqueda de Última Hora (Serper) ---
async def obtener_contexto_real(l_q, v_q):
    if not SERPER_KEY:
        return "No hay API Key de Serper configurada."
    
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

# --- Lógica de validación unificada (usada por /validar y verificador.py) ---
PICKS_VOID = ["no bet", "no apostar", "no apostar (sin valor)", "sin valor"]

def evaluar_resultado(pick, partido, home_name, away_name, winner):
    """
    Determina WIN/LOSS/VOID de forma consistente.
    winner: 'HOME_TEAM' | 'AWAY_TEAM' | 'DRAW'
    """
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

# --- Comando Principal: Pronóstico Suavizado V2 ---
@bot.message_handler(commands=['pronostico', 'valor'])
async def handle_pronostico(message):
    if not SISTEMA_IA["estratega"]["nodo"]:
        await bot.reply_to(message, "🚨 Configura los nodos con `/config`."); return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or " vs " not in parts[1]:
        await bot.reply_to(message, "⚠️ `/pronostico Local vs Visitante`."); return

    l_q, v_q = [t.strip() for t in parts[1].split(" vs ")]
    msg_espera = await bot.reply_to(message, "📡 Ejecutando Análisis Suavizado...")

    try:
        # CORRECCIÓN: llamada async para no bloquear el event loop
        raw_json = await asyncio.to_thread(requests.get, URL_JSON, timeout=10)
        full_data = raw_json.json()
        check_json = raw_json.status_code == 200
    except:
        await bot.edit_message_text("❌ Error al cargar el JSON del servidor.", message.chat.id, msg_espera.message_id); return

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
    h2h, check_h2h, home_wins, away_wins = await obtener_h2h_directo(l_s.get("id_api"), v_s.get("id_api"))

    # --- REGLA 1: Poisson + Mercado (Suavizado 90/10) ---
    avg = full_data[liga]['averages']
    lh = l_s['att_h'] * v_s['def_a'] * avg['league_home']
    la = v_s['att_a'] * l_s['def_h'] * avg['league_away']
    
    prob_poisson = 0
    for x in range(6):
        for y in range(6):
            if x > y: prob_poisson += poisson.pmf(x, lh) * poisson.pmf(y, la)

    prob_market = 1 / c_l
    p_win = (prob_poisson * 0.90) + (prob_market * 0.10)
    p_percent = p_win * 100

    # --- REGLA 2: Edge y Margen de Error Suavizado ---
    edge_real = p_win - prob_market
    margen_error = 0.01
    edge_ajustado = edge_real - margen_error

    # --- REGLA 3: Filtro Cuotas Trampa Flexibilizado ---
    if 1.90 <= c_l <= 2.20 and edge_ajustado < 0.02:
        edge_ajustado = -0.001

    # --- REGLA 5 y 6: Niveles Actualizados ---
    if edge_ajustado <= 0:
        nivel, stake, pick_final = "NO BET 🚫", 0, "No Bet"
    elif edge_ajustado < 0.04:
        nivel, stake, pick_final = "BRONCE 🥉", 0.50, m_l
    elif edge_ajustado < 0.07:
        nivel, stake, pick_final = "PLATA 🥈", 1.00, m_l
    elif edge_ajustado < 0.10:
        nivel, stake, pick_final = "ORO 🥇", 1.50, m_l
    else:
        nivel, stake, pick_final = "DIAMANTE 💎", 2.50, m_l

    # --- REGLA 8: Guardado en Historial ---
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
    # Lógica de Deduplicación
    clave_partido = f"{m_l}_vs_{m_v}_{fecha_hoy[:10]}"
    ahora = datetime.now(timezone.utc)
    if clave_partido in COOLDOWN and (ahora - COOLDOWN[clave_partido]).total_seconds() < COOLDOWN_MINUTOS * 60:
        pass  # No guardar duplicado
    else:
        COOLDOWN[clave_partido] = ahora
        asyncio.create_task(task_github())

    # --- REGLA 9: Prompt IA ---
    header = (f"<b>🛠 REPORTE:</b> {'✅' if check_odds else '❌'} Mercado | "
              f"{'✅' if check_json else '❌'} Poisson ({p_percent:.1f}%) | "
              f"{'✅' if check_h2h else '❌'} H2H\n"
              f"————————————————————\n")
    
    prompt_e = f"""
Eres analista profesional. Partido: {m_l} vs {m_v}
Datos: Prob: {p_percent:.1f}%, Cuota: {c_l}, Edge: {edge_ajustado*100:.2f}%, H2H: {h2h}
Instrucciones: 
1. Si edge <= 0, di NO BET.
2. Si edge > 0, explica por qué hay valor basándote en que Poisson supera al mercado.
3. Máximo 100 palabras.
🎯 PICK: {pick_final}
📈 NIVEL: {nivel}
💰 STAKE: {stake}%
"""
    
    analisis_raw = await ejecutar_ia("estratega", prompt_e)
    analisis = html.escape(analisis_raw)
    footer = f"\n\n{'—'*20}\n🛰 <b>ESTRATEGA:</b> <code>{SISTEMA_IA['estratega']['api']}</code>"

    if SISTEMA_IA["auditor"]["nodo"]:
        prompt_a = (
            f"ERES AUDITOR. Valida: '{analisis_raw}'\n"
            f"NOTICIAS:\n{contexto_noticias}\n"
            f"Resumen muy breve."
        )
        auditoria_raw = await ejecutar_ia("auditor", prompt_a)
        footer += f"\n🛡 <b>AUDITOR:</b> <code>{SISTEMA_IA['auditor']['api']}</code>"
        final = f"{header}{analisis}\n\n{html.escape(auditoria_raw)}{footer}"
    else:
        final = f"{header}{analisis}{footer}"

    await bot.edit_message_text(final, message.chat.id, msg_espera.message_id, parse_mode='HTML')

# --- Comandos Adicionales ---

@bot.message_handler(commands=['historial'])
async def cmd_historial(message):
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}"
    try:
        # CORRECCIÓN: llamada async
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
        # CORRECCIÓN: llamada async
        r_hist = await asyncio.to_thread(requests.get, url_h, timeout=10)
        historial_raw = r_hist.json()
        data_api = await api_football_call("matches?status=FINISHED")
        if not data_api:
            await bot.edit_message_text("❌ Sin resultados nuevos.", message.chat.id, msg_espera.message_id); return
        count = 0
        for item in historial_raw:
            if item.get("status") == "⏳ PENDIENTE":
                for m in data_api['matches']:
                    # CORRECCIÓN: usar 'name' en lugar de 'shortName' para consistencia con verificador.py
                    h_api = m['homeTeam']['name'].lower()
                    a_api = m['awayTeam']['name'].lower()
                    if h_api in item['partido'].lower() and a_api in item['partido'].lower():
                        winner = m['score']['winner']
                        # CORRECCIÓN: usar función unificada evaluar_resultado
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
    # CORRECCIÓN: llamada async
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
        "🤖 <b>SISTEMA V5.2 PRO</b>\n\n"
        "📈 <b>ANÁLISIS:</b>\n"
        "• <code>/pronostico Local vs Visitante</code>: Análisis + Kelly.\n"
        "• <code>/historial</code>: Últimos pronósticos.\n"
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
