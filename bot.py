# -*- coding: utf-8 -*-
import os
import json
import asyncio
import logging
import requests
import base64
import re as _re
import html
from scipy.stats import poisson
from datetime import datetime, timedelta, timezone

import telebot
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from aiohttp import web

# --- Configuración de Entorno ---
logging.basicConfig(level=logging.INFO)
load_dotenv()

TOKEN             = os.getenv('TOKEN_TELEGRAM')
GROQ_KEY          = os.getenv('GROQ_API_KEY')
SAMBA_KEY         = os.getenv('SAMBA_KEY')
FOOTBALL_DATA_KEY = os.getenv('FOOTBALL_DATA_API_KEY')
ODDS_API_KEY      = os.getenv('API_KEY_ODDS')
GITHUB_TOKEN      = os.getenv('GITHUB_TOKEN')
SERPER_KEY        = os.getenv('SERPER_API_KEY')
JINA_KEY          = os.getenv('JINA_API_KEY')
RAPIDAPI_KEY      = os.getenv('RAPIDAPI_KEY')
RAPIDAPI_HOST     = "sportapi7.p.rapidapi.com"

TU_CHAT_ID    = int(os.getenv("CHAT_ID", "0"))
OFFSET_JUAREZ = -6
URL_JSON      = "https://raw.githubusercontent.com/gjoe9955-netizen/claude/main/modelo_poisson.json"
REPO_OWNER    = "gjoe9955-netizen"
REPO_NAME     = "claude"
FILE_PATH     = "historial.json"

# ID de LaLiga en SportAPI7
LALIGA_TOURNAMENT_ID = 8

bot      = AsyncTeleBot(TOKEN)
COOLDOWN = {}
COOLDOWN_MINUTOS = 30

# ── CONSTANTES DE VENTAJA DE CAMPO ─────────────────────────
HOME_ADVANTAGE_FACTOR = 1.10
HOME_ELO_BONUS        = 50
DC_RHO                = -0.13

# ── RAPID HEADERS ───────────────────────────────────────────
RAPID_HEADERS = {
    "X-RapidAPI-Key":  RAPIDAPI_KEY or "",
    "X-RapidAPI-Host": RAPIDAPI_HOST
}

# ============================================================
# CAMBIO 1 — Helper centralizado de errores de API
# ============================================================
def _error_api(nombre_api: str, detalle: str = "", status_code: int = None) -> str:
    """Genera un mensaje de error legible para el usuario."""
    partes = [f"❌ <b>Falló {nombre_api}</b>"]
    if status_code:
        partes.append(f"Código HTTP: <code>{status_code}</code>")
    if detalle:
        partes.append(f"Motivo: <code>{html.escape(str(detalle)[:120])}</code>")
    return "\n".join(partes)


# --- Cache del modelo en memoria ---
_MODELO_CACHE = {"data": None, "ts": None}
CACHE_TTL_SEGUNDOS = 3600

async def obtener_modelo():
    ahora = datetime.now(timezone.utc)
    if _MODELO_CACHE["data"] and _MODELO_CACHE["ts"] and (ahora - _MODELO_CACHE["ts"]).total_seconds() < CACHE_TTL_SEGUNDOS:
        return _MODELO_CACHE["data"], True
    try:
        r = await asyncio.to_thread(requests.get, URL_JSON, timeout=10)
        if r.status_code == 200:
            _MODELO_CACHE["data"] = r.json()
            _MODELO_CACHE["ts"]   = ahora
            return _MODELO_CACHE["data"], True
    except:
        pass
    return _MODELO_CACHE["data"], False


# ============================================================
# SPORTAPI7 — helpers para bot (lineups, goleadores, tarjetas)
# ============================================================

def _similitud(a: str, b: str) -> float:
    palabras_a = set(a.lower().split())
    palabras_b = set(b.lower().split())
    stopwords  = {"fc", "cf", "ud", "cd", "rc", "rcd", "ca", "de", "la", "el", "los", "club"}
    palabras_a -= stopwords
    palabras_b -= stopwords
    if not palabras_a or not palabras_b:
        return 0.0
    comunes = palabras_a & palabras_b
    return len(comunes) / max(len(palabras_a), len(palabras_b))


async def buscar_evento_rapid(nombre_local: str, nombre_visita: str, fecha: str = None) -> int | None:
    if not RAPIDAPI_KEY:
        return None

    if not fecha:
        fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        url = f"https://{RAPIDAPI_HOST}/api/v1/sport/football/scheduled-events/{fecha}"
        r   = await asyncio.to_thread(requests.get, url, headers=RAPID_HEADERS, timeout=15)
        if r.status_code != 200:
            return None

        eventos = r.json().get("events", [])
        mejor_score = 0.0
        mejor_id    = None

        for ev in eventos:
            unique = ev.get("tournament", {}).get("uniqueTournament", {})
            if unique.get("id") != LALIGA_TOURNAMENT_ID:
                continue

            h_name = ev.get("homeTeam", {}).get("name", "")
            a_name = ev.get("awayTeam", {}).get("name", "")

            score_h = _similitud(nombre_local,  h_name)
            score_a = _similitud(nombre_visita, a_name)
            score   = (score_h + score_a) / 2

            if score > mejor_score:
                mejor_score = score
                mejor_id    = ev.get("id")

        if mejor_score >= 0.35 and mejor_id:
            logging.info(f"[SportAPI7] Evento encontrado: id={mejor_id} score={mejor_score:.2f}")
            return mejor_id

    except Exception as e:
        logging.error(f"[SportAPI7] Error buscando evento: {e}")

    return None


async def obtener_lineups_rapid(event_id: int) -> dict:
    if not RAPIDAPI_KEY or not event_id:
        return {}

    try:
        url = f"https://{RAPIDAPI_HOST}/api/v1/event/{event_id}/lineups"
        r   = await asyncio.to_thread(requests.get, url, headers=RAPID_HEADERS, timeout=15)
        if r.status_code != 200:
            return {}

        data = r.json()
        resultado = {"local": [], "visita": []}

        home_data = data.get("home", {})
        for jugador in home_data.get("players", []):
            nombre = jugador.get("player", {}).get("name", "")
            if nombre:
                resultado["local"].append(nombre)

        away_data = data.get("away", {})
        for jugador in away_data.get("players", []):
            nombre = jugador.get("player", {}).get("name", "")
            if nombre:
                resultado["visita"].append(nombre)

        logging.info(f"[SportAPI7] Lineups: local={len(resultado['local'])} visita={len(resultado['visita'])}")
        return resultado

    except Exception as e:
        logging.error(f"[SportAPI7] Error lineups: {e}")
        return {}


async def obtener_goleadores_fd() -> list:
    if not FOOTBALL_DATA_KEY:
        return []

    try:
        url     = "https://api.football-data.org/v4/competitions/PD/scorers?limit=10"
        headers = {"X-Auth-Token": FOOTBALL_DATA_KEY}
        r       = await asyncio.to_thread(requests.get, url, headers=headers, timeout=15)
        if r.status_code != 200:
            return []

        scorers = r.json().get("scorers", [])
        resultado = []
        for s in scorers:
            nombre = s.get("player", {}).get("name", "")
            equipo = s.get("team", {}).get("name", "")
            goles  = s.get("goals", 0) or 0
            if nombre:
                resultado.append({"nombre": nombre, "equipo": equipo, "goles": goles})

        logging.info(f"[FD Scorers] {len(resultado)} goleadores obtenidos.")
        return resultado

    except Exception as e:
        logging.error(f"[FD Scorers] Error: {e}")
        return []


def cruzar_goleadores_lineup(goleadores: list, lineups: dict, nombre_local: str, nombre_visita: str) -> dict:
    resultado = {"local": [], "visita": []}

    if not goleadores:
        return resultado

    for scorer in goleadores:
        nombre_scorer = scorer["nombre"].lower()
        equipo_scorer = scorer["equipo"].lower()
        goles         = scorer["goles"]

        es_local  = (nombre_local.lower()  in equipo_scorer or equipo_scorer in nombre_local.lower())
        es_visita = (nombre_visita.lower() in equipo_scorer or equipo_scorer in nombre_visita.lower())

        if not es_local and not es_visita:
            continue

        if lineups:
            jugadores_equipo = lineups["local"] if es_local else lineups["visita"]
            en_lineup = any(
                nombre_scorer in j.lower() or j.lower() in nombre_scorer
                for j in jugadores_equipo
            )
            if jugadores_equipo and not en_lineup:
                continue

        if es_local:
            resultado["local"].append((scorer["nombre"], goles))
        elif es_visita:
            resultado["visita"].append((scorer["nombre"], goles))

    return resultado


def obtener_tarjetas_del_modelo(full_data: dict, nombre_local: str, nombre_visita: str) -> dict:
    liga   = next(iter(full_data))
    teams  = full_data[liga].get("teams", {})

    match_l = next((t for t in teams if t.lower() in nombre_local.lower() or nombre_local.lower() in t.lower()), None)
    match_v = next((t for t in teams if t.lower() in nombre_visita.lower() or nombre_visita.lower() in t.lower()), None)

    tarjetas_l = teams.get(match_l, {}).get("tarjetas") if match_l else None
    tarjetas_v = teams.get(match_v, {}).get("tarjetas") if match_v else None

    return {
        "local":  tarjetas_l or {"avg_amarillas": 2.1, "avg_rojas": 0.1, "partidos_analizados": 0},
        "visita": tarjetas_v or {"avg_amarillas": 2.1, "avg_rojas": 0.1, "partidos_analizados": 0}
    }


# ============================================================
# BÚSQUEDA DE BAJAS (Serper + Jina)
# ============================================================
PALABRAS_BAJA_LOCAL   = [
    "baja", "lesión", "lesionado", "no jugará", "ausente", "descartado", "out",
    "baja confirmada", "no estará", "se pierde", "fuera de la convocatoria",
    "no disponible", "sancionado", "suspendido"
]
PALABRAS_BAJA_VISITA = PALABRAS_BAJA_LOCAL


async def fetch_jina(url: str) -> str:
    if not JINA_KEY:
        return ""
    try:
        jina_url = f"https://r.jina.ai/{url}"
        headers  = {
            "Authorization": f"Bearer {JINA_KEY}",
            "Accept": "text/plain",
            "X-Return-Format": "text"
        }
        r = await asyncio.to_thread(requests.get, jina_url, headers=headers, timeout=30)
        if r.status_code == 200:
            return r.text[:3000]
    except Exception as e:
        logging.error(f"Error Jina: {e}")
    return ""


async def obtener_contexto_real(l_q, v_q):
    if not SERPER_KEY:
        return "No hay API Key de Serper configurada.", 1.0, 1.0

    url   = "https://google.serper.dev/search"
    query = f'(site:jornadaperfecta.com OR site:futbolfantasy.com) "{l_q}" "{v_q}" alineación'
    payload = json.dumps({"q": query, "gl": "es", "hl": "es", "tbs": "qdr:w"})
    headers = {'X-API-KEY': SERPER_KEY, 'Content-Type': 'application/json'}

    try:
        r   = await asyncio.to_thread(requests.post, url, headers=headers, data=payload, timeout=15)
        res = r.json().get('organic', [])

        if not res:
            return "No se encontraron noticias recientes.", 1.0, 1.0

        urls_top        = [item['link'] for item in res[:2] if item.get('link')]
        contenidos_jina = await asyncio.gather(*[fetch_jina(u) for u in urls_top])

        contexto        = ""
        penalty_local   = 1.0
        penalty_visita  = 1.0

        for i, item in enumerate(res[:3]):
            snippet   = item.get('snippet', '')
            titulo    = item.get('title', '')
            contenido_completo = contenidos_jina[i] if i < len(contenidos_jina) else ""
            texto_analisis     = (contenido_completo if contenido_completo else snippet + " " + titulo).lower()
            contexto          += f"- {titulo}: {snippet}\n"

            if l_q.lower() in texto_analisis:
                if any(p in texto_analisis for p in PALABRAS_BAJA_LOCAL):
                    if any(p in texto_analisis for p in ["delantero", "goleador", "extremo", "mediapunta"]):
                        penalty_local = min(penalty_local, 0.88)
                    elif any(p in texto_analisis for p in ["portero", "defensa", "central", "lateral"]):
                        penalty_local = min(penalty_local, 0.95)
                    else:
                        penalty_local = min(penalty_local, 0.93)
                    if sum(1 for p in PALABRAS_BAJA_LOCAL if p in texto_analisis) >= 3:
                        penalty_local = min(penalty_local, 0.85)

            if v_q.lower() in texto_analisis:
                if any(p in texto_analisis for p in PALABRAS_BAJA_VISITA):
                    if any(p in texto_analisis for p in ["delantero", "goleador", "extremo", "mediapunta"]):
                        penalty_visita = min(penalty_visita, 0.88)
                    elif any(p in texto_analisis for p in ["portero", "defensa", "central", "lateral"]):
                        penalty_visita = min(penalty_visita, 0.95)
                    else:
                        penalty_visita = min(penalty_visita, 0.93)
                    if sum(1 for p in PALABRAS_BAJA_VISITA if p in texto_analisis) >= 3:
                        penalty_visita = min(penalty_visita, 0.85)

        contexto_final = contexto if contexto else "No se encontraron noticias recientes."
        return contexto_final, penalty_local, penalty_visita

    except Exception as e:
        logging.error(f"Error Serper/Jina: {e}")
        return "Error consultando noticias de última hora.", 1.0, 1.0


# --- Persistencia en GitHub ---
async def guardar_en_github(nuevo_registro=None, historial_completo=None):
    url     = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        r_get      = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
        r_get_json = r_get.json()

        if r_get.status_code == 200:
            sha            = r_get_json.get('sha')
            contenido_raw  = r_get_json.get('content', '')
            historial_actual = json.loads(base64.b64decode(contenido_raw.replace('\n', '')).decode('utf-8'))
        elif r_get.status_code == 404:
            sha              = None
            historial_actual = []
        else:
            logging.error(f"GitHub GET falló: {r_get.status_code}")
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

        payload = {"message": "🤖 Actualización de Historial", "content": nuevo_contenido}
        if sha:
            payload["sha"] = sha

        r_put = await asyncio.to_thread(requests.put, url, headers=headers, json=payload, timeout=15)
        if r_put.status_code not in (200, 201):
            logging.error(f"GitHub PUT falló: {r_put.status_code}")
        else:
            logging.info(f"✅ historial.json actualizado ({len(historial)} registros)")

    except Exception as e:
        logging.error(f"Error GitHub: {e}", exc_info=True)


# --- Estado Global IA ---
SISTEMA_IA = {
    "estratega": {"api": None, "nodo": None},
    "auditor":   {"api": None, "nodo": None},

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


async def ejecutar_ia(rol, prompt):
    config   = SISTEMA_IA[rol]
    if not config["nodo"]:
        return None
    nodo_real = config["nodo"].split(" [")[0]

    if config["api"] == 'GROQ':
        url     = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    else:
        url     = "https://api.sambanova.ai/v1/chat/completions"
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


# --- Odds ---
async def obtener_datos_mercado(equipo_l):
    if not ODDS_API_KEY:
        return 1.85, 3.50, 4.00, False, [], (1.85, 1.85), (4.00, 4.00)

    CASAS_PREFERIDAS = {
        "pinnacle", "betfair", "bet365", "williamhill",
        "unibet", "bwin", "betway", "marathonbet"
    }
    MAX_CASAS = 6

    try:
        url    = "https://api.the-odds-api.com/v4/sports/soccer_spain_la_liga/odds/"
        params = {'apiKey': ODDS_API_KEY, 'regions': 'eu', 'markets': 'h2h'}
        r      = await asyncio.to_thread(requests.get, url, params=params, timeout=10)
        if r.status_code != 200:
            return 1.85, 3.50, 4.00, False, [], (1.85, 1.85), (4.00, 4.00)

        for match in r.json():
            home  = match['home_team'].lower()
            query = equipo_l.lower()
            if not (query in home or home in query):
                continue

            bookmakers         = match.get('bookmakers', [])
            bookmakers_ordenados = sorted(bookmakers, key=lambda b: (0 if b['key'] in CASAS_PREFERIDAS else 1))
            ol_list, oe_list, ov_list = [], [], []
            casas_usadas = []

            for bm in bookmakers_ordenados[:MAX_CASAS]:
                try:
                    outcomes = bm['markets'][0]['outcomes']
                    ol = next(o['price'] for o in outcomes if o['name'] == match['home_team'])
                    ov = next(o['price'] for o in outcomes if o['name'] == match['away_team'])
                    oe = next(o['price'] for o in outcomes if o['name'] == 'Draw')
                    ol_list.append(ol); oe_list.append(oe); ov_list.append(ov)
                    casas_usadas.append(bm['key'])
                except (StopIteration, KeyError, IndexError):
                    continue

            if not ol_list:
                return 1.85, 3.50, 4.00, False, [], (1.85, 1.85), (4.00, 4.00)

            ol_consenso = round(sum(ol_list) / len(ol_list), 3)
            oe_consenso = round(sum(oe_list) / len(oe_list), 3)
            ov_consenso = round(sum(ov_list) / len(ov_list), 3)
            rango_l     = (round(min(ol_list), 3), round(max(ol_list), 3))
            rango_v     = (round(min(ov_list), 3), round(max(ov_list), 3))
            return ol_consenso, oe_consenso, ov_consenso, True, casas_usadas, rango_l, rango_v

    except Exception as e:
        logging.error(f"Error obtener_datos_mercado: {e}")

    return 1.85, 3.50, 4.00, False, [], (1.85, 1.85), (4.00, 4.00)


async def obtener_confirmacion_ou(equipo_l, lambda_h, lambda_a):
    if not ODDS_API_KEY:
        return 1.0, "O/U: Sin API"
    try:
        url    = "https://api.the-odds-api.com/v4/sports/soccer_spain_la_liga/odds/"
        params = {'apiKey': ODDS_API_KEY, 'regions': 'eu', 'markets': 'totals'}
        r      = await asyncio.to_thread(requests.get, url, params=params, timeout=15)
        if r.status_code == 200:
            for match in r.json():
                home  = match['home_team'].lower()
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


async def obtener_factor_calibracion():
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}"
    try:
        r = await asyncio.to_thread(requests.get, url, timeout=10)
        if r.status_code != 200:
            return 1.0
        historial    = r.json()
        completados  = [h for h in historial if h.get('status') in ('✅ WIN', '❌ LOSS') and 'poisson' in h]
        if len(completados) < 10:
            return 1.0
        wins         = sum(1 for h in completados if h['status'] == '✅ WIN')
        tasa_real    = wins / len(completados)
        tasa_predicha = sum(float(h['poisson'].replace('%', '')) / 100 for h in completados) / len(completados)
        if tasa_predicha == 0:
            return 1.0
        factor = tasa_real / tasa_predicha
        return max(0.85, min(1.15, factor))
    except:
        return 1.0


# ============================================================
# CAMBIO 2 — api_football_call mejorado con errores descriptivos
# ============================================================
async def api_football_call(endpoint, _raise_on_error=False):
    if not FOOTBALL_DATA_KEY:
        raise ValueError("FOOTBALL_DATA_API_KEY no configurada")
    headers = {'X-Auth-Token': FOOTBALL_DATA_KEY}
    try:
        r = await asyncio.to_thread(
            requests.get,
            f"https://api.football-data.org/v4/competitions/PD/{endpoint}",
            headers=headers, timeout=20
        )
        if r.status_code == 200:
            return r.json()
        if r.status_code == 401:
            raise PermissionError("API Key inválida o sin permisos (401)")
        if r.status_code == 429:
            raise ConnectionError("Límite de peticiones alcanzado (429) — espera unos minutos")
        if r.status_code == 403:
            raise PermissionError("Acceso denegado (403) — verifica tu plan de Football-Data")
        raise ConnectionError(f"HTTP {r.status_code}")
    except (PermissionError, ConnectionError, ValueError):
        raise
    except requests.exceptions.Timeout:
        raise TimeoutError("Timeout (>20s) conectando con Football-Data")
    except Exception as e:
        raise RuntimeError(str(e))


# ============================================================
# Cache de partidos FINISHED
# ============================================================
_PARTIDOS_FD_CACHE = {"data": None, "ts": None}
PARTIDOS_FD_TTL    = 3600


async def obtener_partidos_finished() -> list:
    ahora = datetime.now(timezone.utc)
    if (
        _PARTIDOS_FD_CACHE["data"] is not None and
        _PARTIDOS_FD_CACHE["ts"] and
        (ahora - _PARTIDOS_FD_CACHE["ts"]).total_seconds() < PARTIDOS_FD_TTL
    ):
        return _PARTIDOS_FD_CACHE["data"]

    try:
        data = await api_football_call("matches?status=FINISHED")
        if data and "matches" in data:
            _PARTIDOS_FD_CACHE["data"] = data["matches"]
            _PARTIDOS_FD_CACHE["ts"]   = ahora
            logging.info(f"[FD] ✅ {len(data['matches'])} partidos FINISHED en cache.")
            return data["matches"]
    except Exception as e:
        logging.error(f"[FD] Error partidos FINISHED: {e}")

    return _PARTIDOS_FD_CACHE["data"] or []


# ============================================================
# H2H
# ============================================================
def obtener_h2h_json(id_local: int, id_visita: int, full_data: dict) -> tuple:
    liga        = next(iter(full_data))
    h2h_section = full_data[liga].get("h2h", {})
    clave       = f"{id_local}_{id_visita}"
    entrada     = h2h_section.get(clave)

    if not entrada or not entrada.get("partidos"):
        return "H2H: Sin datos en JSON.", False, 0, 0, 0, "sin_datos"

    partidos           = entrada["partidos"]
    temporadas_con_xg  = entrada.get("temporadas_con_xg", 0)
    total              = len(partidos)
    home_wins, away_wins, empates = 0, 0, 0

    for p in partidos:
        w = p.get("winner")
        if w == "HOME_TEAM":   home_wins += 1
        elif w == "AWAY_TEAM": away_wins += 1
        else:                  empates   += 1

    if temporadas_con_xg >= 6:   fuente_label = "Understat 2T"
    elif temporadas_con_xg >= 1: fuente_label = "Understat 1T"
    elif total >= 1:             fuente_label = f"{total}p sin xG"
    else:                        fuente_label = "sin_datos"

    texto = f"H2H ({total} partidos · {fuente_label})"
    return texto, True, home_wins, away_wins, total, fuente_label


def calcular_factor_h2h(home_wins, away_wins, total_partidos):
    if total_partidos < 1:
        return 1.0, 1.0, "H2H: Sin datos"
    if total_partidos < 5:
        return 1.0, 1.0, f"H2H ⚖️ Muestra insuficiente ({total_partidos}p < 5)"

    tasa_local  = home_wins / total_partidos
    tasa_visita = away_wins / total_partidos
    MAX_AJUSTE  = 0.04

    if tasa_local > 0.60:
        intensidad = min((tasa_local - 0.60) / 0.40, 1.0)
        ajuste     = MAX_AJUSTE * intensidad
        return 1.0 + ajuste, 1.0 - ajuste, f"H2H 🏠 Dominio local ({tasa_local*100:.0f}%, +{ajuste*100:.1f}% lh)"
    elif tasa_visita > 0.60:
        intensidad = min((tasa_visita - 0.60) / 0.40, 1.0)
        ajuste     = MAX_AJUSTE * intensidad
        return 1.0 - ajuste, 1.0 + ajuste, f"H2H 🚩 Dominio visita ({tasa_visita*100:.0f}%, +{ajuste*100:.1f}% la)"
    else:
        return 1.0, 1.0, f"H2H ⚖️ Equilibrado ({home_wins}L/{away_wins}V)"


# ============================================================
# Forma reciente con timeout 20s y reintentos
# ============================================================
async def obtener_forma_reciente(team_id, reintentos: int = 3, delay: float = 2.0):
    if not team_id:
        return 1.0, 1.0, "Forma: Sin ID"

    headers = {'X-Auth-Token': FOOTBALL_DATA_KEY}
    url     = f"https://api.football-data.org/v4/teams/{team_id}/matches?status=FINISHED&limit=5"

    for intento in range(1, reintentos + 1):
        try:
            r = await asyncio.to_thread(requests.get, url, headers=headers, timeout=20)
            if r.status_code != 200:
                return 1.0, 1.0, "Forma: Sin datos"

            matches = r.json().get('matches', [])
            if not matches:
                return 1.0, 1.0, "Forma: Sin partidos"

            puntos = 0
            for m in matches[:5]:
                w       = m['score']['winner']
                home_id = m['homeTeam']['id']
                es_local = (home_id == team_id)
                if (es_local and w == 'HOME_TEAM') or (not es_local and w == 'AWAY_TEAM'):
                    puntos += 3
                elif w == 'DRAW':
                    puntos += 1

            MAX_AJUSTE = 0.10
            forma_norm = puntos / 15.0

            if forma_norm > 0.67:
                intensidad     = (forma_norm - 0.67) / 0.33
                ajuste         = MAX_AJUSTE * intensidad
                factor_ataque  = 1.0 + ajuste
                factor_defensa = 1.0 - ajuste
                simbolo        = "🔥"
            elif forma_norm < 0.33:
                intensidad     = (0.33 - forma_norm) / 0.33
                ajuste         = MAX_AJUSTE * intensidad
                factor_ataque  = 1.0 - ajuste
                factor_defensa = 1.0 + ajuste
                simbolo        = "❄️"
            else:
                factor_ataque  = 1.0
                factor_defensa = 1.0
                simbolo        = "➡️"

            return factor_ataque, factor_defensa, f"Forma {simbolo} {puntos}pts/15 (factor atk ×{factor_ataque:.3f})"

        except requests.exceptions.Timeout:
            logging.warning(f"[Forma] Timeout team {team_id} intento {intento}/{reintentos}")
            if intento < reintentos:
                await asyncio.sleep(delay)
        except Exception as e:
            logging.error(f"[Forma] Error team {team_id}: {e}")
            if intento < reintentos:
                await asyncio.sleep(delay)

    logging.error(f"[Forma] Fallaron {reintentos} intentos para team {team_id}. Usando neutro.")
    return 1.0, 1.0, "Forma: Timeout (neutro)"


async def obtener_posiciones_tabla():
    try:
        data = await api_football_call("standings")
        if not data:
            return {}
        tabla = {}
        for t in data['standings'][0]['table']:
            tabla[t['team']['id']] = {
                'pos':    t['position'],
                'puntos': t['points'],
                'nombre': t['team']['name']
            }
        return tabla
    except Exception as e:
        logging.error(f"Error tabla: {e}")
        return {}


def calcular_factor_tabla(pos_local, pos_visita, pts_local, pts_visita):
    MAX_AJUSTE = 0.06
    diff_pos   = pos_visita - pos_local

    if abs(diff_pos) < 6:
        return 1.0, 1.0, f"Tabla ⚖️ Diferencia leve ({pos_local}° vs {pos_visita}°, {pts_local}pts vs {pts_visita}pts)"

    intensidad = min((abs(diff_pos) - 6) / 14, 1.0)
    ajuste     = MAX_AJUSTE * intensidad

    if diff_pos > 0:
        return 1.0 + ajuste, 1.0 - ajuste, f"Tabla 📈 Local superior ({pos_local}° vs {pos_visita}°, +{ajuste*100:.1f}% lh)"
    else:
        return 1.0 - ajuste, 1.0 + ajuste, f"Tabla 📉 Visita superior ({pos_local}° vs {pos_visita}°, +{ajuste*100:.1f}% la)"


def dixon_coles_tau(x, y, lh, la, rho=DC_RHO):
    if x == 0 and y == 0: return 1.0 - (lh * la * rho)
    if x == 1 and y == 0: return 1.0 + (la * rho)
    if x == 0 and y == 1: return 1.0 + (lh * rho)
    if x == 1 and y == 1: return 1.0 - rho
    return 1.0


# ============================================================
# Cache Elo
# ============================================================
_ELO_CACHE    = {"data": None, "ts": None}
ELO_CACHE_TTL = 3600


async def calcular_elo_equipos(tabla: dict) -> dict:
    ahora = datetime.now(timezone.utc)
    if _ELO_CACHE["data"] and _ELO_CACHE["ts"] and (ahora - _ELO_CACHE["ts"]).total_seconds() < ELO_CACHE_TTL:
        return _ELO_CACHE["data"]

    if not tabla:
        return {}

    elos: dict = {tid: 1500.0 for tid in tabla}
    K       = 32
    matches = await obtener_partidos_finished()

    if not matches:
        _ELO_CACHE["data"] = elos
        _ELO_CACHE["ts"]   = ahora
        return elos

    for m in sorted(matches, key=lambda m: m["utcDate"]):
        h_id   = m["homeTeam"]["id"]
        a_id   = m["awayTeam"]["id"]
        winner = m["score"].get("winner")

        if not winner or h_id not in elos or a_id not in elos:
            continue

        elo_h = elos[h_id]
        elo_a = elos[a_id]
        exp_h = 1 / (1 + 10 ** ((elo_a - elo_h) / 400))
        exp_a = 1 - exp_h

        if winner == "HOME_TEAM":   s_h, s_a = 1.0, 0.0
        elif winner == "AWAY_TEAM": s_h, s_a = 0.0, 1.0
        else:                       s_h, s_a = 0.5, 0.5

        elos[h_id] = elo_h + K * (s_h - exp_h)
        elos[a_id] = elo_a + K * (s_a - exp_a)

    _ELO_CACHE["data"] = elos
    _ELO_CACHE["ts"]   = ahora
    return elos


def calcular_factor_elo(elo_local: float, elo_visita: float) -> tuple:
    MAX_AJUSTE          = 0.08
    MAX_DIFF            = 200.0
    elo_local_ajustado  = elo_local + HOME_ELO_BONUS
    diff                = elo_local_ajustado - elo_visita
    intensidad          = max(-1.0, min(diff / MAX_DIFF, 1.0))
    ajuste              = MAX_AJUSTE * intensidad
    factor_lh           = round(1.0 + ajuste, 4)
    factor_la           = round(1.0 - ajuste, 4)

    if abs(ajuste) < 0.01:
        texto = f"Elo ⚖️ Equilibrado ({elo_local:.0f}+50 vs {elo_visita:.0f})"
    elif diff > 0:
        texto = f"Elo 📈 Local superior ({elo_local:.0f}+50 vs {elo_visita:.0f}, +{ajuste*100:.1f}% lh)"
    else:
        texto = f"Elo 📉 Visita superior ({elo_local:.0f}+50 vs {elo_visita:.0f}, +{abs(ajuste)*100:.1f}% la)"

    return factor_lh, factor_la, texto


def calcular_lambdas_base(l_s: dict, v_s: dict, avg: dict) -> tuple:
    att_local  = (l_s.get('att_h', 1.0) + l_s.get('att_a', 1.0)) / 2 if 'att_h' in l_s else l_s.get('att', 1.0)
    def_local  = (l_s.get('def_h', 1.0) + l_s.get('def_a', 1.0)) / 2 if 'def_h' in l_s else l_s.get('def', 1.0)
    att_visita = (v_s.get('att_h', 1.0) + v_s.get('att_a', 1.0)) / 2 if 'att_h' in v_s else v_s.get('att', 1.0)
    def_visita = (v_s.get('def_h', 1.0) + v_s.get('def_a', 1.0)) / 2 if 'def_h' in v_s else v_s.get('def', 1.0)
    avg_neutro = (avg.get('league_home', 1.5) + avg.get('league_away', 1.2)) / 2

    lh_base = att_local  * def_visita * avg_neutro * HOME_ADVANTAGE_FACTOR
    la_base = att_visita * def_local  * avg_neutro
    return lh_base, la_base


def calcular_shin(odds_l, odds_e, odds_v):
    p_raw     = [1 / odds_l, 1 / odds_e, 1 / odds_v]
    n         = len(p_raw)
    overround = sum(p_raw)
    z         = 0.0
    p_shin    = p_raw[:]

    for _ in range(1000):
        p_shin_nuevo = []
        for p in p_raw:
            discriminante = z ** 2 + 4 * (1 - z) * (p / overround)
            discriminante = max(discriminante, 0.0)
            denom_shin    = 2 * (1 - z)
            if denom_shin == 0:
                p_shin_nuevo.append(p / overround)
            else:
                p_shin_nuevo.append((discriminante ** 0.5 - z) / denom_shin)

        suma   = sum(p_shin_nuevo)
        min_p  = min(p_shin_nuevo)
        denominador = suma - n * min_p
        if denominador == 0:
            break
        z_nuevo = max(0.0, min((suma - 1) / denominador, 0.15))
        if abs(z_nuevo - z) < 1e-9:
            p_shin = p_shin_nuevo
            break
        z      = z_nuevo
        p_shin = p_shin_nuevo

    total  = sum(p_shin)
    p_shin = [p / total for p in p_shin]
    return p_shin[0], p_shin[1], p_shin[2], z


def interpretar_shin(divergencia, z):
    if divergencia < 0.02:
        return "✅ Alta (Shin≈Simple, señal sólida)", 1.0,  "bajo (mercado eficiente)"
    elif divergencia < 0.04:
        return "⚠️ Media (divergencia leve, cautela)", 0.85, "medio"
    else:
        return "🚨 Baja (métodos divergen, señal débil)", 0.70, "alto (posibles insiders)"


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


def calcular_marcadores_probables(lh: float, la: float, top_n: int = 3) -> list:
    resultados = []
    for x in range(6):
        for y in range(6):
            p = poisson.pmf(x, lh) * poisson.pmf(y, la) * dixon_coles_tau(x, y, lh, la)
            resultados.append(((x, y), p))
    resultados.sort(key=lambda r: r[1], reverse=True)
    return resultados[:top_n]


# ============================================================
# Comando Principal: Pronóstico V12
# ============================================================
@bot.message_handler(commands=['pronostico', 'valor'])
async def handle_pronostico(message):
    if TU_CHAT_ID and message.chat.id != TU_CHAT_ID:
        return
    if not SISTEMA_IA["estratega"]["nodo"]:
        await bot.reply_to(message, "🚨 Configura los nodos con `/config`."); return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or " vs " not in parts[1]:
        await bot.reply_to(message, "⚠️ `/pronostico Local vs Visitante`."); return

    l_q, v_q = [t.strip() for t in parts[1].split(" vs ")]
    msg_espera = await bot.reply_to(message, "📡 Ejecutando Análisis V12 (Poisson+DC+H2H+Forma+Tabla+Elo+Odds+Shin+Jina+SportAPI7)...")

    full_data, check_json = await obtener_modelo()
    if not full_data:
        await bot.edit_message_text("❌ Error al cargar el JSON del servidor.", message.chat.id, msg_espera.message_id); return

    liga = next(iter(full_data))
    m_l  = next((t for t in full_data[liga]['teams'] if t.lower() in l_q.lower() or l_q.lower() in t.lower()), None)
    m_v  = next((t for t in full_data[liga]['teams'] if t.lower() in v_q.lower() or v_q.lower() in t.lower()), None)

    if not m_l or not m_v:
        equipo_faltante = l_q if not m_l else v_q
        await bot.edit_message_text(
            f"❌ <b>{html.escape(equipo_faltante)}</b> no está en el modelo.\n"
            f"Puede que haya descendido o el nombre no coincide.\n\n"
            f"Usa /equipos para ver la lista completa.",
            message.chat.id, msg_espera.message_id, parse_mode='HTML'
        )
        return

    l_s  = full_data[liga]['teams'][m_l]
    v_s  = full_data[liga]['teams'][m_v]
    id_l = l_s.get("id_api")
    id_v = v_s.get("id_api")

    (
        (c_l, c_e, c_v, check_odds, casas_usadas, rango_l, rango_v),
        (contexto_noticias, penalty_local, penalty_visita),
        factor_calibracion,
        (forma_local_atk, forma_local_def, forma_local_txt),
        (forma_visita_atk, forma_visita_def, forma_visita_txt),
        tabla,
        goleadores_fd
    ) = await asyncio.gather(
        obtener_datos_mercado(l_q),
        obtener_contexto_real(l_q, v_q),
        obtener_factor_calibracion(),
        obtener_forma_reciente(id_l),
        obtener_forma_reciente(id_v),
        obtener_posiciones_tabla(),
        obtener_goleadores_fd()
    )

    event_id = await buscar_evento_rapid(m_l, m_v)
    lineups  = await obtener_lineups_rapid(event_id) if event_id else {}

    tarjetas = obtener_tarjetas_del_modelo(full_data, m_l, m_v)
    tarj_l   = tarjetas["local"]
    tarj_v   = tarjetas["visita"]

    goleadores_partido = cruzar_goleadores_lineup(goleadores_fd, lineups, m_l, m_v)

    h2h, check_h2h, home_wins, away_wins, total_h2h, fuente_h2h = obtener_h2h_json(id_l, id_v, full_data)
    elos = await calcular_elo_equipos(tabla)
    avg  = full_data[liga]['averages']

    lh_base, la_base = calcular_lambdas_base(l_s, v_s, avg)
    factor_lh_h2h, factor_la_h2h, h2h_texto = calcular_factor_h2h(home_wins, away_wins, total_h2h)

    factor_lh_tabla, factor_la_tabla, tabla_texto = 1.0, 1.0, "Tabla: Sin datos"
    if tabla and id_l in tabla and id_v in tabla:
        factor_lh_tabla, factor_la_tabla, tabla_texto = calcular_factor_tabla(
            tabla[id_l]['pos'], tabla[id_v]['pos'], tabla[id_l]['puntos'], tabla[id_v]['puntos']
        )

    factor_lh_elo, factor_la_elo, elo_texto = 1.0, 1.0, "Elo: Sin datos"
    if elos and id_l in elos and id_v in elos:
        factor_lh_elo, factor_la_elo, elo_texto = calcular_factor_elo(elos[id_l], elos[id_v])

    factor_total_lh = factor_lh_h2h * forma_local_atk  * factor_lh_tabla * factor_lh_elo * penalty_local
    factor_total_la = factor_la_h2h * forma_visita_atk * factor_la_tabla * factor_la_elo * penalty_visita

    lh = lh_base * (1 + (factor_total_lh - 1) * 0.7)
    la = la_base * (1 + (factor_total_la - 1) * 0.7)

    prob_poisson = prob_poisson_empate = prob_poisson_visita = 0
    for x in range(7):
        for y in range(7):
            p = poisson.pmf(x, lh) * poisson.pmf(y, la) * dixon_coles_tau(x, y, lh, la)
            if x > y:   prob_poisson        += p
            elif x == y: prob_poisson_empate += p
            else:        prob_poisson_visita += p

    prob_poisson_calibrado_local  = prob_poisson        * factor_calibracion
    prob_poisson_empate_cal       = prob_poisson_empate * factor_calibracion
    prob_poisson_visita_cal       = prob_poisson_visita * factor_calibracion

    overround       = (1 / c_l) + (1 / c_e) + (1 / c_v)
    prob_simple_l   = (1 / c_l) / overround
    prob_simple_e   = (1 / c_e) / overround
    prob_simple_v   = (1 / c_v) / overround
    shin_l, shin_e, shin_v, shin_z = calcular_shin(c_l, c_e, c_v)
    divergencia_shin = abs(shin_l - prob_simple_l)
    shin_confianza, shin_factor, shin_z_txt = interpretar_shin(divergencia_shin, shin_z)

    prob_market_l = (prob_simple_l + shin_l) / 2
    prob_market_e = (prob_simple_e + shin_e) / 2
    prob_market_v = (prob_simple_v + shin_v) / 2

    ou_factor, ou_texto = await obtener_confirmacion_ou(l_q, lh, la)

    margen_error   = 0.003 + (shin_z * 0.02)
    edge_local_raw = (prob_poisson_calibrado_local - prob_market_l - margen_error) * shin_factor
    edge_empate    = (prob_poisson_empate_cal       - prob_market_e - margen_error) * shin_factor
    edge_visita    = (prob_poisson_visita_cal        - prob_market_v - margen_error) * shin_factor

    edge_ajustado = edge_local_raw
    if 1.90 <= c_l <= 2.10 and edge_ajustado < 0.02: edge_ajustado = -0.001
    if 1.90 <= c_e <= 2.10 and edge_empate   < 0.02: edge_empate   = -0.001
    if 1.90 <= c_v <= 2.10 and edge_visita   < 0.02: edge_visita   = -0.001

    if c_e < 3.0:
        edge_ajustado *= 0.80; edge_empate *= 0.80; edge_visita *= 0.80
        empate_aviso = f"⚠️ Cuota empate baja ({c_e:.2f}) → edge reducido 20% en los 3 resultados"
    else:
        empate_aviso = f"Cuota empate: {c_e:.2f} ✅"

    zona_gris = (
        abs(prob_poisson_calibrado_local - prob_market_l) < 0.03 and
        abs(prob_poisson_empate_cal - prob_market_e) < 0.03 and
        abs(prob_poisson_visita_cal - prob_market_v) < 0.03
    )

    candidatos = []
    if edge_ajustado > 0: candidatos.append(("local",  edge_ajustado, c_l, m_l,     prob_poisson_calibrado_local * 100))
    if edge_empate   > 0: candidatos.append(("empate", edge_empate,   c_e, "Empate", prob_poisson_empate_cal * 100))
    if edge_visita   > 0: candidatos.append(("visita", edge_visita,   c_v, m_v,      prob_poisson_visita_cal * 100))

    pick_riesgo_nombre = None
    stake_riesgo       = 0
    nivel_riesgo       = ""
    edge_riesgo        = 0
    prob_riesgo        = 0
    pick_riesgo_cuota  = 0

    if candidatos and not zona_gris:
        candidatos.sort(key=lambda c: c[1], reverse=True)
        tipo_pick, edge_principal, cuota_pick, nombre_pick, prob_pick_pct = candidatos[0]

        kelly_fraccion    = 0.25 * shin_factor
        kelly_full        = edge_principal / (cuota_pick - 1)
        kelly_fraccionado = kelly_full * kelly_fraccion
        stake_base        = round(kelly_fraccionado * 100, 2)
        stake             = round(stake_base * ou_factor, 2)
        stake             = max(0.25, min(stake, 3.0))
        pick_final        = nombre_pick
        p_percent         = prob_pick_pct

        if stake < 0.75:     nivel = "BRONCE 🥉"
        elif stake < 1.25:   nivel = "PLATA 🥈"
        elif stake < 2.0:    nivel = "ORO 🥇"
        else:                nivel = "DIAMANTE 💎"

        if len(candidatos) > 1:
            _, edge_riesgo, pick_riesgo_cuota, pick_riesgo_nombre, prob_riesgo = candidatos[1]
            kelly_riesgo  = (edge_riesgo / (pick_riesgo_cuota - 1)) * 0.25
            stake_riesgo  = round(min(max(kelly_riesgo * 100, 0.25), 1.0), 2)
            if stake_riesgo < 0.50:   nivel_riesgo = "RIESGO BAJO ⚠️"
            elif stake_riesgo < 0.75: nivel_riesgo = "RIESGO MEDIO 🎲"
            else:                     nivel_riesgo = "RIESGO ALTO 🔴"
    else:
        nivel, stake, pick_final = "NO BET 🚫", 0, "No Bet"
        ou_factor     = 1.0
        tipo_pick     = "ninguno"
        cuota_pick    = 0
        prob_pick_pct = 0
        edge_principal = 0
        nombre_pick   = "No Bet"
        p_percent     = 0

        top_scores = calcular_marcadores_probables(lh, la, top_n=3)
        if top_scores:
            marcador_top       = top_scores[0][0]
            prob_riesgo        = top_scores[0][1] * 100
            pick_riesgo_nombre = f"{marcador_top[0]}-{marcador_top[1]}"
            stake_riesgo       = 0.25
            nivel_riesgo       = "RIESGO ALTO 🔴"

    fecha_hoy = (datetime.now(timezone.utc) + timedelta(hours=OFFSET_JUAREZ)).strftime('%Y-%m-%d %H:%M')
    registro  = {
        "fecha":        fecha_hoy,
        "partido":      f"{m_l} vs {m_v}",
        "pick":         pick_final,
        "poisson":      f"{p_percent:.1f}%",
        "cuota":        cuota_pick if pick_final != "No Bet" else c_l,
        "edge":         f"{edge_principal*100:.1f}%" if pick_final != "No Bet" else "0.0%",
        "stake":        f"{stake}%",
        "nivel":        nivel,
        "pick_riesgo":  pick_riesgo_nombre if pick_riesgo_nombre else "Sin valor alternativo",
        "stake_riesgo": f"{stake_riesgo}%" if stake_riesgo else "0%",
        "status":       "⏳ PENDIENTE"
    }

    clave_partido = f"{m_l}_vs_{m_v}_{fecha_hoy[:10]}"
    ahora_ts      = datetime.now(timezone.utc)
    ya_en_ram     = (
        clave_partido in COOLDOWN and
        (ahora_ts - COOLDOWN[clave_partido]).total_seconds() < COOLDOWN_MINUTOS * 60
    )

    async def ya_en_github():
        try:
            r = await asyncio.to_thread(requests.get,
                f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}", timeout=10)
            if r.status_code != 200:
                return False
            return any(
                h.get("partido") == f"{m_l} vs {m_v}" and h.get("fecha", "")[:10] == fecha_hoy[:10]
                for h in r.json()
            )
        except:
            return False

    if ya_en_ram:
        logging.info(f"[Cooldown RAM] Ignorando duplicado {clave_partido}")
    else:
        if not await ya_en_github():
            COOLDOWN[clave_partido] = ahora_ts
            asyncio.create_task(guardar_en_github(nuevo_registro=registro))

    def _fmt_tarjetas(t: dict, fuente: str) -> str:
        partidos_ok = t.get("partidos_analizados", 0)
        fuente_txt  = f"{partidos_ok}p analizados" if partidos_ok > 0 else "fallback liga"
        return f"🟨 {t['avg_amarillas']:.1f} amarillas · 🟥 {t['avg_rojas']:.1f} rojas/partido ({fuente_txt})"

    tarjetas_block = (
        f"\n<b>◆ TARJETAS ESPERADAS</b>\n"
        f"<b>🏠 {html.escape(m_l)}:</b> {_fmt_tarjetas(tarj_l, 'local')}\n"
        f"<b>🚩 {html.escape(m_v)}:</b> {_fmt_tarjetas(tarj_v, 'visita')}\n"
        f"<i>Total partido: ~{tarj_l['avg_amarillas'] + tarj_v['avg_amarillas']:.1f} amarillas · ~{tarj_l['avg_rojas'] + tarj_v['avg_rojas']:.1f} rojas</i>\n"
    )

    def _fmt_goleadores(lista: list) -> str:
        if not lista:
            return "Sin datos"
        return " · ".join(f"{nombre} ({goles}⚽)" for nombre, goles in lista[:3])

    lineup_txt = ""
    if lineups:
        n_local  = len(lineups.get("local",  []))
        n_visita = len(lineups.get("visita", []))
        lineup_txt = f"\n<b>◆ ALINEACIONES</b> <i>(SportAPI7)</i>\n"
        if n_local > 0:
            lineup_txt += f"<b>🏠</b> {html.escape(', '.join(lineups['local'][:11]))}\n"
        if n_visita > 0:
            lineup_txt += f"<b>🚩</b> {html.escape(', '.join(lineups['visita'][:11]))}\n"
    else:
        lineup_txt = "\n<i>⚠️ Alineaciones no disponibles aún</i>\n"

    goleadores_block = (
        f"\n<b>◆ GOLEADORES PROBABLES</b>\n"
        f"<b>🏠</b> {html.escape(_fmt_goleadores(goleadores_partido['local']))}\n"
        f"<b>🚩</b> {html.escape(_fmt_goleadores(goleadores_partido['visita']))}\n"
    )

    calib_txt     = f"{factor_calibracion:.2f}" if factor_calibracion != 1.0 else "1.00 (sin datos suficientes)"
    h2h_ajuste_txt = f"+{(factor_lh_h2h-1)*100:.1f}%lh" if factor_lh_h2h != 1.0 else (f"+{(factor_la_h2h-1)*100:.1f}%la" if factor_la_h2h != 1.0 else "sin ajuste")
    serper_txt    = ""
    if penalty_local  < 1.0: serper_txt += f" ⚠️ Bajas local (-{(1-penalty_local)*100:.0f}%lh)"
    if penalty_visita < 1.0: serper_txt += f" ⚠️ Bajas visita (-{(1-penalty_visita)*100:.0f}%la)"
    if not serper_txt: serper_txt = " Sin bajas detectadas"

    zona_gris_txt = " · 🌫 Zona gris (mercado eficiente)" if zona_gris else ""
    tipo_emoji    = {"local": "🏠", "empate": "🤝", "visita": "🚩"}.get(tipo_pick if pick_final != "No Bet" else "", "")

    if pick_final == "No Bet":
        if pick_riesgo_nombre:
            decision_block = (
                f"<b>╔{'═'*22}╗</b>\n"
                f"<b>║  🚫 NO BET — SIN VALOR      ║</b>\n"
                f"<b>╠{'═'*22}╣</b>\n"
                f"<b>║  {nivel_riesgo:<22}║</b>\n"
                f"<b>║  🎲 Marcador: {pick_riesgo_nombre:<15}║</b>\n"
                f"<b>║  📊 Prob Poisson: {prob_riesgo:.1f}%{' '*(10-len(f'{prob_riesgo:.1f}'))}║</b>\n"
                f"<b>║  💰 Stake: {stake_riesgo}% (solo riesgo){' '*(5-len(str(stake_riesgo)))}║</b>\n"
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

    p_local_pct  = prob_poisson_calibrado_local * 100
    p_empate_pct = prob_poisson_empate_cal * 100
    p_visita_pct = prob_poisson_visita_cal * 100
    n_elo        = len([m for m in (_PARTIDOS_FD_CACHE.get("data") or []) if m["score"].get("winner")])

    signals_block = (
        f"\n<b>◆ SEÑALES</b>\n"
        f"<code>"
        f"Poisson  🏠 {p_local_pct:.1f}%  🤝 {p_empate_pct:.1f}%  🚩 {p_visita_pct:.1f}%\n"
        f"λH {lh:.2f}  λA {la:.2f}\n"
        f"Edge     🏠 {edge_ajustado*100:.1f}%  🤝 {edge_empate*100:.1f}%  🚩 {edge_visita*100:.1f}%\n"
        f"Mercado  Simple 🏠{prob_simple_l*100:.1f}% 🤝{prob_simple_e*100:.1f}% 🚩{prob_simple_v*100:.1f}%\n"
        f"Shin z   {shin_z:.4f}  {html.escape(shin_confianza[:22])}\n"
        f"Cuotas   L {c_l}  E {c_e}  V {c_v}  OR {overround:.3f}  {'(consenso)' if check_odds else '(default)'}\n"
        f"Rango L  [{rango_l[0]}-{rango_l[1]}]  Rango V [{rango_v[0]}-{rango_v[1]}]\n"
        f"Casas    {html.escape(', '.join(casas_usadas) if casas_usadas else 'default')}\n"
        f"Calib    x{html.escape(calib_txt)}  {html.escape(ou_texto[:28])}\n"
        f"Empate   {html.escape(empate_aviso[:38])}\n"
        f"Margen   {margen_error*100:.3f}%{html.escape(zona_gris_txt)}"
        f"</code>\n"
    )

    context_block = (
        f"\n<b>◆ CONTEXTO</b>\n"
        f"<b>H2H</b> {html.escape(h2h_texto)} → {html.escape(h2h_ajuste_txt)}\n"
        f"<b>🏠</b> {html.escape(forma_local_txt)}\n"
        f"<b>🚩</b> {html.escape(forma_visita_txt)}\n"
        f"<b>🏆</b> {html.escape(tabla_texto)}\n"
        f"<b>⚡</b> {html.escape(elo_texto)} <i>({n_elo} partidos)</i>\n"
        f"<b>📰</b>{html.escape(serper_txt)}\n"
        f"\n<b>◆ ANÁLISIS  {'✅' if check_odds else '❌'} Odds · {'✅' if check_json else '❌'} Poisson · {'✅' if check_h2h else '⚠️'} H2H</b>\n"
    )

    header = decision_block + signals_block + context_block + tarjetas_block + lineup_txt + goleadores_block

    gol_local_txt  = _fmt_goleadores(goleadores_partido["local"])
    gol_visita_txt = _fmt_goleadores(goleadores_partido["visita"])

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
• Factor calibración histórica: ×{factor_calibracion:.2f}
• Zona gris detectada: {'SÍ — modelo y mercado alineados (diferencia < 3%)' if zona_gris else 'No'}

── MERCADO DE CUOTAS ──
• Cuota local: {c_l} → prob. implícita bruta: {(1/c_l)*100:.1f}%
• Cuota empate: {c_e} → prob. implícita bruta: {(1/c_e)*100:.1f}%
• Cuota visitante: {c_v} → prob. implícita bruta: {(1/c_v)*100:.1f}%
• Overround: {overround:.4f} | Casas: {', '.join(casas_usadas) if casas_usadas else 'default'}

── MÉTODO SHIN ──
• Prob. local Shin: {shin_l*100:.1f}% | Empate: {shin_e*100:.1f}% | Visita: {shin_v*100:.1f}%
• Parámetro z: {shin_z:.4f} → {shin_z_txt}
• Divergencia: {divergencia_shin*100:.2f}% | Confianza: {shin_confianza}

── EDGE ──
• Edge local: {edge_ajustado*100:.2f}% | Empate: {edge_empate*100:.2f}% | Visitante: {edge_visita*100:.2f}%

── PICK SELECCIONADO ──
• PICK PRINCIPAL: {pick_final} | Nivel: {nivel} | Stake: {stake}%
• {ou_texto} | {empate_aviso}

── PICK DE RIESGO ──
• Marcador más probable (Poisson): {pick_riesgo_nombre if pick_riesgo_nombre else "N/A"} ({prob_riesgo:.1f}%)
• Stake: {stake_riesgo}% (tope 0.25%, solo riesgo consciente)

── H2H ──
• {h2h} | Ajuste: {h2h_ajuste_txt}
• Victorias local: {home_wins} | Visita: {away_wins} | Total: {total_h2h}

── FORMA RECIENTE ──
• Local: {forma_local_txt}
• Visita: {forma_visita_txt}

── TABLA ──
• {tabla_texto}

── ELO ──
• {elo_texto}

── BAJAS (SERPER + JINA) ──
• {serper_txt}
• {contexto_noticias[:800]}

── TARJETAS PROMEDIO ──
• {m_l}: {tarj_l['avg_amarillas']:.1f} amarillas | {tarj_l['avg_rojas']:.1f} rojas/partido ({tarj_l.get('partidos_analizados',0)} partidos)
• {m_v}: {tarj_v['avg_amarillas']:.1f} amarillas | {tarj_v['avg_rojas']:.1f} rojas/partido ({tarj_v.get('partidos_analizados',0)} partidos)
• Total partido esperado: {tarj_l['avg_amarillas']+tarj_v['avg_amarillas']:.1f} amarillas | {tarj_l['avg_rojas']+tarj_v['avg_rojas']:.1f} rojas

── GOLEADORES PROBABLES (FD Scorers + Lineup) ──
• {m_l}: {gol_local_txt}
• {m_v}: {gol_visita_txt}
• Alineación confirmada: {'SÍ (' + str(len(lineups.get('local',[]))) + '+' + str(len(lineups.get('visita',[]))) + ' jugadores)' if lineups else 'No disponible aún'}

═══════════════════════════════════════
PICK PRINCIPAL: {pick_final} | NIVEL: {nivel} | STAKE: {stake}%
PICK RIESGO (marcador): {pick_riesgo_nombre if pick_riesgo_nombre else "N/A"} ({prob_riesgo:.1f}%)
═══════════════════════════════════════

INSTRUCCIONES:
Redacta un análisis de máximo 260 palabras que integre:
a) Por qué el pick tiene o no valor (edge vs margen dinámico).
b) Shin vs normalización simple: convergencia o divergencia.
c) Forma reciente y tabla: refuerzan o contradicen el pronóstico.
d) H2H histórico: tamaño de muestra y dirección.
e) Bajas detectadas y su impacto en lambdas.
f) Over/Under: confirma o contradice la dirección.
g) Tarjetas esperadas: si el partido apunta a ser tenso o fluido.
h) Goleador más probable si hay dato disponible.
Sé directo, técnico y conciso. No repitas los números del header, interprétalos.
"""

    analisis_raw = await ejecutar_ia("estratega", prompt_e)
    analisis     = html.escape(_re.sub(r"<[^>]+>", "", analisis_raw or ""))
    nodos_txt    = f"🛰 <code>{SISTEMA_IA['estratega']['api']}</code>"

    if SISTEMA_IA["auditor"]["nodo"]:
        prompt_a = (
            f"ERES AUDITOR INDEPENDIENTE. Evalúa si el pick es correcto con los datos crudos.\n\n"
            f"PARTIDO: {m_l} vs {m_v}\n"
            f"Pick: {pick_final} | Nivel: {nivel} | Stake: {stake}%\n"
            f"Edge local: {edge_ajustado*100:.2f}% | Empate: {edge_empate*100:.2f}% | Visita: {edge_visita*100:.2f}%\n"
            f"Prob modelo: L {p_local_pct:.1f}% E {p_empate_pct:.1f}% V {p_visita_pct:.1f}%\n"
            f"λH={lh:.2f} | λA={la:.2f} | Shin z={shin_z:.4f} | {shin_confianza}\n"
            f"Tarjetas: {m_l} {tarj_l['avg_amarillas']:.1f}am/{tarj_l['avg_rojas']:.1f}r | {m_v} {tarj_v['avg_amarillas']:.1f}am/{tarj_v['avg_rojas']:.1f}r\n"
            f"Goleadores: L={gol_local_txt} | V={gol_visita_txt}\n"
            f"Pick riesgo: {pick_riesgo_nombre if pick_riesgo_nombre else 'N/A'} ({prob_riesgo:.1f}%)\n"
            f"H2H: {h2h} | Forma: {forma_local_txt} / {forma_visita_txt}\n"
            f"Bajas: {serper_txt}\n\n"
            f"En máximo 90 palabras: ¿Los datos respaldan o contradicen el pick? "
            f"¿El marcador Poisson es coherente con λH/λA? "
            f"¿Las tarjetas esperadas sugieren mercado alternativo? "
            f"Señala la contradicción más importante si existe."
        )
        auditoria_raw  = await ejecutar_ia("auditor", prompt_a)
        nodos_txt     += f" · 🛡 <code>{SISTEMA_IA['auditor']['api']}</code>"
        auditor_block  = f"\n\n<b>◆ AUDITOR</b>\n{html.escape(_re.sub(r'<[^>]+>', '', auditoria_raw or ''))}"
    else:
        auditor_block = ""

    footer = f"\n\n<i>{'—'*18}\nV12 · {nodos_txt} · ⚙️ Gwero 👷‍♂️</i>"
    final  = f"{header}{analisis}{auditor_block}{footer}"

    await bot.edit_message_text(final, message.chat.id, msg_espera.message_id, parse_mode='HTML')


# ============================================================
# CAMBIO 3 — Comandos con mensajes de error mejorados
# ============================================================

@bot.message_handler(commands=['stats'])
async def cmd_stats(message):
    if TU_CHAT_ID and message.chat.id != TU_CHAT_ID:
        return
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}"
    try:
        r        = await asyncio.to_thread(requests.get, url, timeout=10)
        historial = r.json()
        if not historial:
            await bot.reply_to(message, "📭 Sin historial."); return

        completados = [h for h in historial if h.get('status') in ('✅ WIN', '❌ LOSS')]
        voided      = [h for h in historial if h.get('status') == '➖ VOID']
        pendientes  = [h for h in historial if h.get('status') == '⏳ PENDIENTE']

        if not completados:
            await bot.reply_to(message, "📊 Sin resultados completos aún."); return

        wins          = sum(1 for h in completados if h['status'] == '✅ WIN')
        pct_aciertos  = (wins / len(completados)) * 100
        roi_total     = 0
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

        roi_pct    = (roi_total / invertido_total * 100) if invertido_total > 0 else 0
        racha      = 0
        racha_tipo = ""
        for h in reversed(completados):
            if racha == 0:
                racha_tipo = h['status']
                racha      = 1
            elif h['status'] == racha_tipo:
                racha += 1
            else:
                break

        racha_emoji  = "🔥" if racha_tipo == "✅ WIN" else "❄️"
        niveles_stats = {}
        for h in completados:
            niv = h.get('nivel', 'Desconocido').split(' ')[0]
            if niv not in niveles_stats:
                niveles_stats[niv] = {'w': 0, 'l': 0}
            if h['status'] == '✅ WIN': niveles_stats[niv]['w'] += 1
            else:                       niveles_stats[niv]['l'] += 1

        desglose = ""
        for niv, datos in niveles_stats.items():
            total_niv = datos['w'] + datos['l']
            pct_niv   = (datos['w'] / total_niv * 100) if total_niv > 0 else 0
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
        await bot.reply_to(message,
            f"❌ <b>Falló GitHub</b> (lectura de historial)\nMotivo: <code>{html.escape(str(e)[:120])}</code>",
            parse_mode='HTML')


@bot.message_handler(commands=['historial'])
async def cmd_historial(message):
    if TU_CHAT_ID and message.chat.id != TU_CHAT_ID:
        return
    url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}"
    try:
        r        = await asyncio.to_thread(requests.get, url, timeout=10)
        historial = r.json()
        if not historial:
            await bot.reply_to(message, "📭 Historial vacío."); return
        txt = "📜 <b>HISTORIAL RECIENTE:</b>\n\n"
        for r_item in historial[-10:]:
            txt += f"📅 <code>{r_item['fecha']}</code>\n⚽ <b>{r_item['partido']}</b>\n🎯 Pick: <code>{r_item['pick']}</code> | {r_item['status']}\n{'—'*15}\n"
        await bot.reply_to(message, txt, parse_mode='HTML')
    except Exception as e:
        logging.error(f"Error /historial: {e}")
        await bot.reply_to(message,
            f"❌ <b>Falló GitHub</b> (lectura de historial)\nMotivo: <code>{html.escape(str(e)[:120])}</code>",
            parse_mode='HTML')


@bot.message_handler(commands=['validar'])
async def cmd_validar(message):
    if TU_CHAT_ID and message.chat.id != TU_CHAT_ID:
        return
    msg_espera = await bot.reply_to(message, "🔍 Sincronizando resultados...")
    url_h = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/{FILE_PATH}"
    try:
        r_hist = await asyncio.to_thread(requests.get, url_h, timeout=10)
        if r_hist.status_code != 200:
            await bot.edit_message_text(
                f"❌ <b>Falló GitHub</b>\nMotivo: <code>HTTP {r_hist.status_code} al leer historial</code>",
                message.chat.id, msg_espera.message_id, parse_mode='HTML'); return

        historial_raw = r_hist.json()
        try:
            data_api = await api_football_call("matches?status=FINISHED")
        except TimeoutError:
            await bot.edit_message_text(
                "❌ <b>Falló Football-Data</b>\nMotivo: <code>Timeout (>20s)</code>",
                message.chat.id, msg_espera.message_id, parse_mode='HTML'); return
        except PermissionError as e:
            await bot.edit_message_text(
                f"❌ <b>Falló Football-Data</b>\nMotivo: <code>{html.escape(str(e))}</code>",
                message.chat.id, msg_espera.message_id, parse_mode='HTML'); return
        except Exception as e:
            await bot.edit_message_text(
                f"❌ <b>Falló Football-Data</b>\nMotivo: <code>{html.escape(str(e)[:120])}</code>",
                message.chat.id, msg_espera.message_id, parse_mode='HTML'); return

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
                if any(p in h_api or h_api in p for p in partido_lower.split(" vs ")[0:1]) and \
                   any(p in a_api or a_api in p for p in partido_lower.split(" vs ")[1:2]):
                    winner = m['score'].get('winner')
                    if not winner:
                        continue
                    item['status']       = evaluar_resultado(item['pick'], item['partido'], h_api, a_api, winner)
                    item['marcador_real'] = f"{m['score']['fullTime']['home']}-{m['score']['fullTime']['away']}"
                    count += 1
                    break

        if count > 0:
            await guardar_en_github(historial_completo=historial_raw)
            await bot.edit_message_text(f"✅ {count} partido(s) validado(s) y guardados.", message.chat.id, msg_espera.message_id)
        else:
            await bot.edit_message_text("ℹ️ No hay partidos nuevos por actualizar.", message.chat.id, msg_espera.message_id)
    except Exception as e:
        logging.error(f"Error /validar: {e}", exc_info=True)
        await bot.edit_message_text(
            f"❌ <b>Fallo en validación</b>\nMotivo: <code>{html.escape(str(e)[:120])}</code>",
            message.chat.id, msg_espera.message_id, parse_mode='HTML')


@bot.message_handler(commands=['partidos'])
async def cmd_partidos(message):
    if TU_CHAT_ID and message.chat.id != TU_CHAT_ID:
        return
    from collections import defaultdict
    try:
        data = await api_football_call("matches?status=SCHEDULED")
    except TimeoutError:
        await bot.reply_to(message,
            "❌ <b>Falló Football-Data</b>\nMotivo: <code>Timeout — servidor tardó más de 20s</code>",
            parse_mode='HTML'); return
    except PermissionError as e:
        await bot.reply_to(message,
            f"❌ <b>Falló Football-Data</b>\nMotivo: <code>{html.escape(str(e))}</code>",
            parse_mode='HTML'); return
    except Exception as e:
        await bot.reply_to(message,
            f"❌ <b>Falló Football-Data</b>\nMotivo: <code>{html.escape(str(e)[:120])}</code>",
            parse_mode='HTML'); return

    if not data:
        await bot.reply_to(message, "❌ <b>Football-Data</b> no devolvió datos.", parse_mode='HTML'); return

    matches = data['matches'][:10]
    if not matches:
        await bot.reply_to(message, "📭 No hay partidos programados."); return

    dias_es = {
        'Monday': 'Lunes', 'Tuesday': 'Martes', 'Wednesday': 'Miércoles',
        'Thursday': 'Jueves', 'Friday': 'Viernes', 'Saturday': 'Sábado', 'Sunday': 'Domingo'
    }
    por_fecha = defaultdict(list)
    for m in matches:
        dt        = datetime.strptime(m['utcDate'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(hours=OFFSET_JUAREZ)
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
    if TU_CHAT_ID and message.chat.id != TU_CHAT_ID:
        return
    try:
        data = await api_football_call("standings")
    except TimeoutError:
        await bot.reply_to(message,
            "❌ <b>Falló Football-Data</b>\nMotivo: <code>Timeout (>20s)</code>",
            parse_mode='HTML'); return
    except PermissionError as e:
        await bot.reply_to(message,
            f"❌ <b>Falló Football-Data</b>\nMotivo: <code>{html.escape(str(e))}</code>",
            parse_mode='HTML'); return
    except Exception as e:
        await bot.reply_to(message,
            f"❌ <b>Falló Football-Data</b>\nMotivo: <code>{html.escape(str(e)[:120])}</code>",
            parse_mode='HTML'); return

    if not data:
        await bot.reply_to(message, "❌ Football-Data no devolvió datos.", parse_mode='HTML'); return

    txt = "🏆 <b>POSICIONES:</b>\n\n"
    for t in data['standings'][0]['table'][:12]:
        txt += f"<code>{t['position']:02d}.</code> <b>{t['team']['shortName']}</b> | {t['points']} pts\n"
    await bot.reply_to(message, txt, parse_mode='HTML')


@bot.message_handler(commands=['equipos'])
async def cmd_equipos(message):
    if TU_CHAT_ID and message.chat.id != TU_CHAT_ID:
        return
    try:
        r   = await asyncio.to_thread(requests.get, URL_JSON, timeout=10)
        res = r.json()
        liga    = next(iter(res))
        equipos = ", ".join([f"<code>{e}</code>" for e in res[liga]['teams'].keys()])
        await bot.reply_to(message, f"📋 <b>EQUIPOS JSON:</b>\n\n{equipos}", parse_mode='HTML')
    except Exception as e:
        await bot.reply_to(message,
            f"❌ <b>Falló Modelo JSON</b>\nMotivo: <code>{html.escape(str(e)[:120])}</code>",
            parse_mode='HTML')


@bot.message_handler(commands=['config'])
async def cmd_config(message):
    if TU_CHAT_ID and message.chat.id != TU_CHAT_ID:
        return
    markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🧠 ASIGNAR ESTRATEGA", callback_data="set_rol_estratega"))
    await bot.reply_to(message, "🛠 <b>CONFIGURACIÓN DE RED</b>", reply_markup=markup, parse_mode='HTML')


@bot.callback_query_handler(func=lambda call: call.data.startswith('set_rol_'))
async def cb_rol(call):
    rol    = call.data.split('_')[-1]
    markup = InlineKeyboardMarkup().row(
        InlineKeyboardButton("Groq",      callback_data=f"set_api_{rol}_GROQ"),
        InlineKeyboardButton("SambaNova", callback_data=f"set_api_{rol}_SAMBA")
    )
    await bot.edit_message_text(f"API para {rol.upper()}:", call.message.chat.id, call.message.message_id, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('set_api_'))
async def cb_api(call):
    _, _, rol, api = call.data.split('_')
    nodos  = SISTEMA_IA["nodos_groq"] if api == 'GROQ' else SISTEMA_IA["nodos_samba"]
    markup = InlineKeyboardMarkup()
    for idx, nombre in enumerate(nodos):
        markup.add(InlineKeyboardButton(nombre, callback_data=f"sv_{rol[0]}_{api[0]}_{idx}"))
    await bot.edit_message_text(f"Selecciona Nodo para {rol.upper()}:", call.message.chat.id, call.message.message_id, reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('sv_'))
async def cb_save(call):
    _, r_init, a_init, idx = call.data.split('_')
    rol     = "estratega" if r_init == 'e' else "auditor"
    api     = "GROQ" if a_init == 'G' else "SAMBA"
    lista   = SISTEMA_IA["nodos_groq"] if api == "GROQ" else SISTEMA_IA["nodos_samba"]
    nodo_sel = lista[int(idx)]
    SISTEMA_IA[rol] = {"api": api, "nodo": nodo_sel}
    markup  = InlineKeyboardMarkup()
    if rol == "estratega":
        markup.add(InlineKeyboardButton("⚖️ AÑADIR AUDITOR", callback_data="set_rol_auditor"))
    markup.add(InlineKeyboardButton("🏁 FINALIZAR", callback_data="config_fin"))
    await bot.edit_message_text(f"✅ {rol.upper()} listo: <code>{nodo_sel}</code>", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='HTML')


@bot.callback_query_handler(func=lambda call: call.data == "config_fin")
async def cb_fin(call):
    await bot.edit_message_text("🚀 <b>SISTEMA LISTO</b>", call.message.chat.id, call.message.message_id, parse_mode='HTML')


# ============================================================
# /live — Análisis in-play con Poisson ajustado
# ============================================================
async def obtener_partido_inplay(l_q: str, v_q: str):
    try:
        data = await api_football_call("matches?status=IN_PLAY,PAUSED")
        if not data or "matches" not in data:
            return None
        for m in data["matches"]:
            h = m["homeTeam"]["name"].lower()
            a = m["awayTeam"]["name"].lower()
            if (l_q.lower() in h or h in l_q.lower()) and (v_q.lower() in a or a in v_q.lower()):
                return m
    except Exception as e:
        logging.error(f"[Live] Error: {e}")
    return None


async def obtener_cuotas_live(l_q: str):
    if not ODDS_API_KEY:
        return None, None, None, "sin_api", None
    try:
        url    = "https://api.the-odds-api.com/v4/sports/soccer_spain_la_liga/odds/"
        params = {'apiKey': ODDS_API_KEY, 'regions': 'eu', 'markets': 'h2h'}
        r      = await asyncio.to_thread(requests.get, url, params=params, timeout=10)
        if r.status_code != 200:
            return None, None, None, "error_api", None

        ahora = datetime.now(timezone.utc)
        for match in r.json():
            h = match['home_team'].lower()
            if not (l_q.lower() in h or h in l_q.lower()):
                continue
            bms = match.get('bookmakers', [])
            if not bms:
                continue
            try:
                ts        = datetime.fromisoformat(bms[0].get('last_update', '').replace('Z', '+00:00'))
                delay_min = (ahora - ts).total_seconds() / 60
            except:
                delay_min = 999

            ol_list, oe_list, ov_list = [], [], []
            for bm in bms[:6]:
                try:
                    outcomes = bm['markets'][0]['outcomes']
                    ol = next(o['price'] for o in outcomes if o['name'] == match['home_team'])
                    ov = next(o['price'] for o in outcomes if o['name'] == match['away_team'])
                    oe = next(o['price'] for o in outcomes if o['name'] == 'Draw')
                    ol_list.append(ol); oe_list.append(oe); ov_list.append(ov)
                except:
                    continue

            if not ol_list:
                continue

            c_l  = round(sum(ol_list)/len(ol_list), 3)
            c_e  = round(sum(oe_list)/len(oe_list), 3)
            c_v  = round(sum(ov_list)/len(ov_list), 3)
            tipo = "live" if delay_min < 12 else "pre-partido"
            return c_l, c_e, c_v, tipo, round(delay_min, 1)

    except Exception as e:
        logging.error(f"[Live Odds] Error: {e}")
    return None, None, None, "sin_datos", None


def calcular_poisson_live(lh_base, la_base, minuto, goles_local, goles_visita):
    minutos_restantes = max(90 - minuto, 1)
    escala   = minutos_restantes / 90
    lh_live  = lh_base * escala
    la_live  = la_base * escala

    prob_local_gana = prob_empate = prob_visita_gana = 0.0
    for x in range(8):
        for y in range(8):
            p       = poisson.pmf(x, lh_live) * poisson.pmf(y, la_live) * dixon_coles_tau(x, y, lh_live, la_live)
            total_l = goles_local  + x
            total_v = goles_visita + y
            if total_l > total_v:   prob_local_gana  += p
            elif total_l == total_v: prob_empate      += p
            else:                    prob_visita_gana += p

    total = prob_local_gana + prob_empate + prob_visita_gana
    if total > 0:
        prob_local_gana  /= total
        prob_empate      /= total
        prob_visita_gana /= total

    return lh_live, la_live, prob_local_gana, prob_empate, prob_visita_gana


@bot.message_handler(commands=['live'])
async def handle_live(message):
    if TU_CHAT_ID and message.chat.id != TU_CHAT_ID:
        return
    if not SISTEMA_IA["estratega"]["nodo"]:
        await bot.reply_to(message, "🚨 Configura los nodos con `/config`."); return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or " vs " not in parts[1]:
        await bot.reply_to(message, "⚠️ `/live Local vs Visitante` o `/live Local vs Visitante 67`"); return

    resto          = parts[1].strip()
    minuto_usuario = None
    tokens         = resto.rsplit(maxsplit=1)
    if len(tokens) == 2 and tokens[1].isdigit():
        minuto_usuario = int(tokens[1])
        resto          = tokens[0]

    if " vs " not in resto:
        await bot.reply_to(message, "⚠️ `/live Local vs Visitante`"); return

    l_q, v_q   = [t.strip() for t in resto.split(" vs ", 1)]
    msg_espera = await bot.reply_to(message, "📡 Buscando partido en curso...")

    partido = await obtener_partido_inplay(l_q, v_q)
    if not partido:
        await bot.edit_message_text("❌ No se encontró ese partido en curso. Verifica con /partidos.", message.chat.id, msg_espera.message_id); return

    goles_local  = partido["score"]["fullTime"]["home"] or 0
    goles_visita = partido["score"]["fullTime"]["away"] or 0
    m_l = partido["homeTeam"]["name"]
    m_v = partido["awayTeam"]["name"]

    if minuto_usuario is not None:
        minuto       = max(1, min(minuto_usuario, 89))
        minuto_fuente = f"min {minuto} (ingresado)"
    else:
        try:
            minuto_api = int(partido.get("minute", 0))
            if minuto_api > 0:
                minuto        = minuto_api
                minuto_fuente = f"min {minuto} (API)"
            else:
                raise ValueError("sin minuto")
        except:
            await bot.edit_message_text(
                f"📡 Partido: <b>{html.escape(m_l)} {goles_local}-{goles_visita} {html.escape(m_v)}</b>\n"
                f"⚠️ La API no devolvió el minuto. ¿En qué minuto van?",
                message.chat.id, msg_espera.message_id, parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup().row(
                    InlineKeyboardButton("Min 15", callback_data=f"live_min_{l_q}|{v_q}|15"),
                    InlineKeyboardButton("Min 30", callback_data=f"live_min_{l_q}|{v_q}|30"),
                    InlineKeyboardButton("Min 45", callback_data=f"live_min_{l_q}|{v_q}|45"),
                ).row(
                    InlineKeyboardButton("Min 60", callback_data=f"live_min_{l_q}|{v_q}|60"),
                    InlineKeyboardButton("Min 75", callback_data=f"live_min_{l_q}|{v_q}|75"),
                    InlineKeyboardButton("Min 85", callback_data=f"live_min_{l_q}|{v_q}|85"),
                )
            )
            return

    await _ejecutar_live(message.chat.id, msg_espera.message_id, l_q, v_q, m_l, m_v, goles_local, goles_visita, minuto, minuto_fuente)


@bot.callback_query_handler(func=lambda call: call.data.startswith('live_min_'))
async def cb_live_minuto(call):
    _, _, datos = call.data.partition('live_min_')
    partes      = datos.split('|')
    if len(partes) != 3:
        return
    l_q, v_q, minuto_str = partes
    minuto  = int(minuto_str)
    partido = await obtener_partido_inplay(l_q, v_q)
    if not partido:
        await bot.edit_message_text("❌ Partido no encontrado.", call.message.chat.id, call.message.message_id); return

    goles_local  = partido["score"]["fullTime"]["home"] or 0
    goles_visita = partido["score"]["fullTime"]["away"] or 0
    m_l = partido["homeTeam"]["name"]
    m_v = partido["awayTeam"]["name"]

    await bot.edit_message_text(f"⚙️ Calculando análisis live min {minuto}...", call.message.chat.id, call.message.message_id)
    await _ejecutar_live(call.message.chat.id, call.message.message_id, l_q, v_q, m_l, m_v, goles_local, goles_visita, minuto, f"min {minuto} (seleccionado)")


async def _ejecutar_live(chat_id, msg_id, l_q, v_q, m_l, m_v, goles_local, goles_visita, minuto, minuto_fuente):
    full_data, _ = await obtener_modelo()
    if not full_data:
        await bot.edit_message_text("❌ Error al cargar modelo JSON.", chat_id, msg_id); return

    liga  = next(iter(full_data))
    eq_l  = next((t for t in full_data[liga]['teams'] if t.lower() in m_l.lower() or m_l.lower() in t.lower()), None)
    eq_v  = next((t for t in full_data[liga]['teams'] if t.lower() in m_v.lower() or m_v.lower() in t.lower()), None)

    if not eq_l or not eq_v:
        await bot.edit_message_text("❌ Equipos no encontrados en el JSON.", chat_id, msg_id); return

    l_s     = full_data[liga]['teams'][eq_l]
    v_s     = full_data[liga]['teams'][eq_v]
    avg     = full_data[liga]['averages']
    lh_base, la_base = calcular_lambdas_base(l_s, v_s, avg)

    c_l, c_e, c_v, tipo_cuota, delay_min = await obtener_cuotas_live(l_q)
    if not c_l:
        c_l, c_e, c_v = 1.85, 3.50, 4.00
        tipo_cuota    = "default"
        delay_min     = None

    lh_live, la_live, p_local, p_empate, p_visita = calcular_poisson_live(lh_base, la_base, minuto, goles_local, goles_visita)

    overround = (1/c_l) + (1/c_e) + (1/c_v)
    pm_l = (1/c_l) / overround
    pm_e = (1/c_e) / overround
    pm_v = (1/c_v) / overround

    edge_l = p_local  - pm_l
    edge_e = p_empate - pm_e
    edge_v = p_visita - pm_v

    candidatos = []
    if edge_l > 0.03: candidatos.append(("local",  edge_l, c_l, eq_l,    p_local*100))
    if edge_e > 0.03: candidatos.append(("empate", edge_e, c_e, "Empate", p_empate*100))
    if edge_v > 0.03: candidatos.append(("visita", edge_v, c_v, eq_v,     p_visita*100))

    if candidatos:
        candidatos.sort(key=lambda x: x[1], reverse=True)
        tipo_pick, edge_pick, cuota_pick, nombre_pick, prob_pick = candidatos[0]
        kelly      = round(min(max((edge_pick / (cuota_pick - 1)) * 0.25 * 100, 0.25), 2.0), 2)
        tipo_emoji = {"local": "🏠", "empate": "🤝", "visita": "🚩"}.get(tipo_pick, "")
        pick_txt   = f"{tipo_emoji} {nombre_pick}"
        nivel_live = "ORO 🥇" if kelly >= 1.25 else ("PLATA 🥈" if kelly >= 0.75 else "BRONCE 🥉")
    else:
        pick_txt   = "🚫 Sin valor detectado"
        edge_pick  = 0; cuota_pick = 0; prob_pick = 0; kelly = 0
        nivel_live = "NO BET"

    cuota_tipo_txt = (
        f"({'⚡live' if tipo_cuota == 'live' else '⚠️pre-partido' if tipo_cuota == 'pre-partido' else '❌default'}, delay {delay_min}min)"
        if delay_min is not None else f"({tipo_cuota})"
    )

    prompt_live = f"""
Eres analista de fútbol in-play. Analiza el partido EN CURSO con los datos ajustados al minuto actual.

PARTIDO EN CURSO: {m_l} {goles_local}-{goles_visita} {m_v} | {minuto_fuente}
Minutos restantes: {90 - minuto}

── POISSON LIVE ──
• λH live: {lh_live:.2f} | λA live: {la_live:.2f}
• λH base (90min): {lh_base:.2f} | λA base (90min): {la_base:.2f}
• Prob. local gana: {p_local*100:.1f}% | Empate: {p_empate*100:.1f}% | Visita: {p_visita*100:.1f}%

── CUOTAS {cuota_tipo_txt} ──
• Local: {c_l} → {pm_l*100:.1f}% | Empate: {c_e} → {pm_e*100:.1f}% | Visita: {c_v} → {pm_v*100:.1f}%

── EDGE LIVE ──
• L: {edge_l*100:.1f}% | E: {edge_e*100:.1f}% | V: {edge_v*100:.1f}%

── PICK LIVE ──
• Pick: {pick_txt} | Nivel: {nivel_live} | Stake: {kelly}%

En máximo 150 palabras: valor real del pick dado marcador y minutos restantes, y si λH/λA live reflejan correctamente el ritmo del partido.
{'AVISO: cuotas con delay ' + str(delay_min) + ' min.' if tipo_cuota == 'pre-partido' else ''}
"""

    analisis_live_raw = await ejecutar_ia("estratega", prompt_live)
    analisis_live     = html.escape(_re.sub(r'<[^>]+>', '', analisis_live_raw or ""))

    header_live = (
        f"<b>⚡ ANÁLISIS LIVE</b>\n"
        f"<b>{html.escape(m_l)} {goles_local} - {goles_visita} {html.escape(m_v)}</b>\n"
        f"<i>{html.escape(minuto_fuente)} | {90-minuto} min restantes</i>\n\n"
        f"<b>╔{'═'*22}╗</b>\n"
        f"<b>║  {nivel_live:<22}║</b>\n"
        f"<b>║  {html.escape(pick_txt):<22}║</b>\n"
        f"<b>║  💰 Stake: {kelly}% Kelly{' '*12}║</b>\n"
        f"<b>╚{'═'*22}╝</b>\n\n"
        f"<code>"
        f"Poisson  🏠 {p_local*100:.1f}%  🤝 {p_empate*100:.1f}%  🚩 {p_visita*100:.1f}%\n"
        f"λH {lh_live:.2f} (base {lh_base:.2f})  λA {la_live:.2f} (base {la_base:.2f})\n"
        f"Edge     🏠 {edge_l*100:.1f}%  🤝 {edge_e*100:.1f}%  🚩 {edge_v*100:.1f}%\n"
        f"Cuotas   L {c_l}  E {c_e}  V {c_v}  {html.escape(cuota_tipo_txt)}\n"
        f"</code>\n\n"
        f"<b>◆ ANÁLISIS LIVE</b>\n"
    )

    final_live = header_live + analisis_live + f"\n\n<i>{'—'*18}\nV12-Live · 🛰 <code>{SISTEMA_IA['estratega']['api']}</code> · ⚙️ gjoe9955</i>"
    await bot.edit_message_text(final_live, chat_id, msg_id, parse_mode='HTML')


# ============================================================
# CAMBIO 4 — Comando /diagnostico
# ============================================================
@bot.message_handler(commands=['diagnostico'])
async def cmd_diagnostico(message):
    if TU_CHAT_ID and message.chat.id != TU_CHAT_ID:
        return
    msg = await bot.reply_to(message, "🔬 Probando todas las APIs...")
    resultados = []

    # 1. Football-Data
    try:
        r = await asyncio.to_thread(
            requests.get,
            "https://api.football-data.org/v4/competitions/PD/standings",
            headers={'X-Auth-Token': FOOTBALL_DATA_KEY or ''}, timeout=10
        )
        if r.status_code == 200:
            resultados.append("✅ <b>Football-Data</b> — OK")
        elif r.status_code == 401:
            resultados.append("❌ <b>Football-Data</b> — API Key inválida (401)")
        elif r.status_code == 429:
            resultados.append("⚠️ <b>Football-Data</b> — Límite alcanzado (429)")
        else:
            resultados.append(f"❌ <b>Football-Data</b> — HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        resultados.append("❌ <b>Football-Data</b> — Timeout (>10s)")
    except Exception as e:
        resultados.append(f"❌ <b>Football-Data</b> — {html.escape(str(e)[:80])}")

    # 2. Odds API
    try:
        r = await asyncio.to_thread(
            requests.get,
            "https://api.the-odds-api.com/v4/sports/",
            params={'apiKey': ODDS_API_KEY or ''}, timeout=10
        )
        if r.status_code == 200:
            resultados.append("✅ <b>Odds API</b> — OK")
        elif r.status_code == 401:
            resultados.append("❌ <b>Odds API</b> — API Key inválida (401)")
        elif r.status_code == 422:
            resultados.append("❌ <b>Odds API</b> — Key no configurada (422)")
        else:
            resultados.append(f"❌ <b>Odds API</b> — HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        resultados.append("❌ <b>Odds API</b> — Timeout (>10s)")
    except Exception as e:
        resultados.append(f"❌ <b>Odds API</b> — {html.escape(str(e)[:80])}")

    # 3. Serper
    try:
        r = await asyncio.to_thread(
            requests.post,
            "https://google.serper.dev/search",
            headers={'X-API-KEY': SERPER_KEY or '', 'Content-Type': 'application/json'},
            data=json.dumps({"q": "test", "gl": "es"}), timeout=10
        )
        if r.status_code == 200:
            resultados.append("✅ <b>Serper</b> — OK")
        elif r.status_code == 403:
            resultados.append("❌ <b>Serper</b> — API Key inválida (403)")
        elif r.status_code == 429:
            resultados.append("⚠️ <b>Serper</b> — Límite alcanzado (429)")
        else:
            resultados.append(f"❌ <b>Serper</b> — HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        resultados.append("❌ <b>Serper</b> — Timeout (>10s)")
    except Exception as e:
        resultados.append(f"❌ <b>Serper</b> — {html.escape(str(e)[:80])}")

    # 4. Groq
    try:
        r = await asyncio.to_thread(
            requests.get,
            "https://api.groq.com/openai/v1/models",
            headers={'Authorization': f'Bearer {GROQ_KEY or ""}', 'Content-Type': 'application/json'},
            timeout=10
        )
        if r.status_code == 200:
            resultados.append("✅ <b>Groq</b> — OK")
        elif r.status_code == 401:
            resultados.append("❌ <b>Groq</b> — API Key inválida (401)")
        elif r.status_code == 429:
            resultados.append("⚠️ <b>Groq</b> — Límite de tokens/min alcanzado (429)")
        else:
            resultados.append(f"❌ <b>Groq</b> — HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        resultados.append("❌ <b>Groq</b> — Timeout (>10s)")
    except Exception as e:
        resultados.append(f"❌ <b>Groq</b> — {html.escape(str(e)[:80])}")

    # 5. SambaNova
    try:
        r = await asyncio.to_thread(
            requests.get,
            "https://api.sambanova.ai/v1/models",
            headers={'Authorization': f'Bearer {SAMBA_KEY or ""}', 'Content-Type': 'application/json'},
            timeout=10
        )
        if r.status_code == 200:
            resultados.append("✅ <b>SambaNova</b> — OK")
        elif r.status_code == 401:
            resultados.append("❌ <b>SambaNova</b> — API Key inválida (401)")
        else:
            resultados.append(f"❌ <b>SambaNova</b> — HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        resultados.append("❌ <b>SambaNova</b> — Timeout (>10s)")
    except Exception as e:
        resultados.append(f"❌ <b>SambaNova</b> — {html.escape(str(e)[:80])}")

    # 6. SportAPI7 (RapidAPI)
    try:
        hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r   = await asyncio.to_thread(
            requests.get,
            f"https://{RAPIDAPI_HOST}/api/v1/sport/football/scheduled-events/{hoy}",
            headers=RAPID_HEADERS, timeout=10
        )
        if r.status_code == 200:
            resultados.append("✅ <b>SportAPI7</b> — OK")
        elif r.status_code == 403:
            resultados.append("❌ <b>SportAPI7</b> — RapidAPI Key inválida (403)")
        elif r.status_code == 429:
            resultados.append("⚠️ <b>SportAPI7</b> — Límite de peticiones (429)")
        else:
            resultados.append(f"❌ <b>SportAPI7</b> — HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        resultados.append("❌ <b>SportAPI7</b> — Timeout (>10s)")
    except Exception as e:
        resultados.append(f"❌ <b>SportAPI7</b> — {html.escape(str(e)[:80])}")

    # 7. GitHub
    try:
        r = await asyncio.to_thread(
            requests.get,
            f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{FILE_PATH}",
            headers={"Authorization": f"token {GITHUB_TOKEN or ''}", "Accept": "application/vnd.github.v3+json"},
            timeout=10
        )
        if r.status_code == 200:
            resultados.append("✅ <b>GitHub</b> — OK")
        elif r.status_code == 401:
            resultados.append("❌ <b>GitHub</b> — Token inválido (401)")
        elif r.status_code == 404:
            resultados.append("⚠️ <b>GitHub</b> — Repo/archivo no encontrado (404)")
        else:
            resultados.append(f"❌ <b>GitHub</b> — HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        resultados.append("❌ <b>GitHub</b> — Timeout (>10s)")
    except Exception as e:
        resultados.append(f"❌ <b>GitHub</b> — {html.escape(str(e)[:80])}")

    # 8. Modelo JSON (GitHub raw)
    try:
        r = await asyncio.to_thread(requests.get, URL_JSON, timeout=10)
        if r.status_code == 200:
            resultados.append("✅ <b>Modelo JSON</b> — OK")
        else:
            resultados.append(f"❌ <b>Modelo JSON</b> — HTTP {r.status_code}")
    except requests.exceptions.Timeout:
        resultados.append("❌ <b>Modelo JSON</b> — Timeout (>10s)")
    except Exception as e:
        resultados.append(f"❌ <b>Modelo JSON</b> — {html.escape(str(e)[:80])}")

    # Nodos IA configurados
    e_nodo = SISTEMA_IA["estratega"]["nodo"]
    a_nodo = SISTEMA_IA["auditor"]["nodo"]
    resultados.append(
        f"\n<b>🧠 Nodos IA:</b>\n"
        f"  Estratega: <code>{'✅ ' + html.escape(e_nodo) if e_nodo else '❌ No configurado'}</code>\n"
        f"  Auditor:   <code>{'✅ ' + html.escape(a_nodo) if a_nodo else '⚠️ No configurado'}</code>"
    )

    ok    = sum(1 for r in resultados if r.startswith("✅"))
    total = 8
    resumen = f"{'✅' if ok == total else '⚠️' if ok >= 5 else '❌'} <b>{ok}/{total} APIs operativas</b>"

    txt_final = f"🔬 <b>DIAGNÓSTICO DE APIS</b>\n{'━'*22}\n" + "\n".join(resultados) + f"\n{'━'*22}\n{resumen}"
    await bot.edit_message_text(txt_final, message.chat.id, msg.message_id, parse_mode='HTML')


@bot.message_handler(commands=['help'])
async def cmd_help(message):
    if TU_CHAT_ID and message.chat.id != TU_CHAT_ID:
        return
    help_text = (
        "🤖 <b>SISTEMA V12.0 PRO</b>\n\n"
        "📈 <b>ANÁLISIS:</b>\n"
        "• <code>/pronostico Local vs Visitante</code>: Poisson+DC+H2H+Forma+Tabla+Elo+Odds+Shin+Kelly+Tarjetas+Goleadores.\n"
        "• <code>/live Local vs Visitante</code>: Análisis in-play con Poisson ajustado a minuto y marcador.\n"
        "• <code>/historial</code>: Últimos pronósticos.\n"
        "• <code>/stats</code>: ROI, % aciertos, racha y desglose por nivel.\n"
        "• <code>/validar</code>: Sincroniza resultados GitHub.\n"
        "• <code>/diagnostico</code>: Prueba todas las APIs y reporta cuál falla y por qué.\n"
        "• <code>/config</code>: Configura IA.\n\n"
        "🛡 <b>ROLES:</b>\n"
        "• <b>[EST]:</b> Estratega (Análisis matemático y Kelly).\n"
        "• <b>[AUD]:</b> Auditor (Verificación lógica).\n\n"
        "⚽ <b>INFORMACIÓN:</b>\n"
        "• <code>/partidos</code>: Próximos encuentros.\n"
        "• <code>/tabla</code>: Posiciones liga.\n"
        "• <code>/equipos</code>: Lista equipos JSON.\n\n"
        "🆕 <b>V12 — NOVEDADES:</b>\n"
        "• Tarjetas amarillas/rojas esperadas por partido.\n"
        "• Goleadores probables cruzados con alineación.\n"
        "• Alineaciones confirmadas (SportAPI7).\n"
        "• Fix timeout/reintentos en forma reciente.\n"
        "• Mensaje de error mejorado para equipos descendidos.\n"
        "• Errores de API detallados en todos los comandos.\n"
        "• /diagnostico para prueba completa de todas las APIs.\n"
    )
    await bot.reply_to(message, help_text, parse_mode='HTML')


# ============================================================
# Webhook & Servidor
# ============================================================
WEBHOOK_HOST = "claude-production-e098.up.railway.app"
WEBHOOK_PATH = f"/{TOKEN}"
WEBHOOK_URL  = f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT         = int(os.getenv("PORT", 8080))


async def handle_webhook(request):
    try:
        data   = await request.json()
        update = telebot.types.Update.de_json(data)
        await bot.process_new_updates([update])
    except Exception as e:
        logging.error(f"[Webhook] Error: {e}")
    return web.Response(text="OK")


async def handle_health(request):
    return web.Response(text="OK")


async def main():
    await bot.remove_webhook()
    await asyncio.sleep(1)
    await bot.set_webhook(url=WEBHOOK_URL)
    logging.info(f"[Arranque] Webhook: {WEBHOOK_URL}")

    logging.info("[Arranque] Precalentando cache FINISHED...")
    await obtener_partidos_finished()
    logging.info("[Arranque] ✅ Cache lista.")

    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.router.add_get("/", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"[Arranque] Puerto {PORT}")

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
