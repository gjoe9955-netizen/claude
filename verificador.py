import os
import json
import requests
import base64
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
API_KEY_FOOTBALL = os.getenv('FOOTBALL_DATA_API_KEY')
REPO_PATH = "gjoe9955-netizen/claude"
HISTORIAL_FILE = "historial.json"

# Mapeo de nombres API → fragmentos clave para cruce seguro
# Evita falsos positivos (ej. "Valencia" vs "Valencia B")
NOMBRE_CLAVE = {
    "real madrid cf": "real madrid",
    "fc barcelona": "barcelona",
    "club atlético de madrid": "atlético de madrid",
    "athletic club": "athletic club",
    "real sociedad de fútbol": "real sociedad",
    "villarreal cf": "villarreal",
    "real betis balompié": "real betis",
    "sevilla fc": "sevilla fc",
    "valencia cf": "valencia cf",
    "rayo vallecano de madrid": "rayo vallecano",
    "getafe cf": "getafe",
    "girona fc": "girona",
    "deportivo alavés": "alavés",
    "ca osasuna": "osasuna",
    "rc celta de vigo": "celta",
    "rcd mallorca": "mallorca",
    "rcd espanyol de barcelona": "espanyol",
    "ud las palmas": "las palmas",
    "real valladolid cf": "valladolid",
    "cd leganés": "leganés",
    "levante ud": "levante",
    "real oviedo": "oviedo",
}

def normalizar(nombre_api):
    """Devuelve la clave de cruce para un nombre de equipo de la API."""
    key = nombre_api.strip().lower()
    return NOMBRE_CLAVE.get(key, key)

def partido_coincide(partido_historial, home_api, away_api):
    """
    Comprueba si un partido del historial corresponde al partido de la API.
    Usa claves normalizadas para evitar falsos positivos.
    """
    partido_lower = partido_historial.lower()
    home_clave = normalizar(home_api)
    away_clave = normalizar(away_api)
    return home_clave in partido_lower and away_clave in partido_lower

def obtener_resultados_recientes():
    url = "https://api.football-data.org/v4/competitions/PD/matches?status=FINISHED"
    headers = {"X-Auth-Token": API_KEY_FOOTBALL}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json().get('matches', [])
        return []
    except Exception as e:
        print(f"❌ Error al consultar API: {e}")
        return []

def actualizar_historial():
    if not GITHUB_TOKEN:
        print("❌ Error: No se encontró GITHUB_TOKEN.")
        return

    url_gh = f"https://api.github.com/repos/{REPO_PATH}/contents/{HISTORIAL_FILE}"
    headers_gh = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    try:
        r_gh = requests.get(url_gh, headers=headers_gh)
        if r_gh.status_code != 200:
            print("❌ No se pudo obtener el historial de GitHub.")
            return

        file_data = r_gh.json()
        contenido_raw = file_data['content'].replace('\n', '')
        historial = json.loads(base64.b64decode(contenido_raw).decode('utf-8'))

        partidos_api = obtener_resultados_recientes()
        cambio = False

        for pick in historial:
            if pick.get("status") == "⏳ PENDIENTE":
                for match in partidos_api:
                    home_api = match['homeTeam']['name']
                    away_api = match['awayTeam']['name']

                    if partido_coincide(pick['partido'], home_api, away_api):
                        goles_l = match['score']['fullTime']['home']
                        goles_v = match['score']['fullTime']['away']
                        marcador_real = f"{goles_l}-{goles_v}"
                        resultado = match['score']['winner']

                        pick["marcador_real"] = marcador_real

                        PICKS_VOID = ["no bet", "no apostar", "no apostar (sin valor)", "sin valor"]
                        pick_lower = pick['pick'].lower()
                        home_clave = normalizar(home_api)
                        away_clave = normalizar(away_api)

                        if any(v in pick_lower for v in PICKS_VOID):
                            pick["status"] = "➖ VOID"
                        elif resultado == 'HOME_TEAM' and home_clave in pick_lower:
                            pick["status"] = "✅ WIN"
                        elif resultado == 'AWAY_TEAM' and away_clave in pick_lower:
                            pick["status"] = "✅ WIN"
                        elif resultado == 'DRAW' and "empate" in pick_lower:
                            pick["status"] = "✅ WIN"
                        else:
                            pick["status"] = "❌ LOSS"

                        cambio = True
                        print(f"✅ Auditado: {pick['partido']} -> {marcador_real} -> {pick['status']}")
                        break  # evitar doble match por partido

        if cambio:
            json_str = json.dumps(historial, indent=4, ensure_ascii=False)
            new_content = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
            payload = {
                "message": "Auditoría automática de resultados",
                "content": new_content,
                "sha": file_data['sha']
            }
            res_put = requests.put(url_gh, headers=headers_gh, json=payload)
            if res_put.status_code == 200:
                print("🚀 GitHub actualizado con los resultados reales.")
            else:
                print(f"❌ Error al subir: {res_put.text}")
        else:
            print("ℹ️ Nada nuevo que auditar.")

    except Exception as e:
        print(f"❌ Error general: {e}")

if __name__ == "__main__":
    actualizar_historial()
