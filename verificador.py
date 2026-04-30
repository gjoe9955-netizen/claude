import os
import json
import requests
import base64
from dotenv import load_dotenv

load_dotenv()

# --- Configuración Sincronizada ---
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
# CORRECCIÓN: Usar el nombre de Railway para consistencia
API_KEY_FOOTBALL = os.getenv('FOOTBALL_DATA_API_KEY') 
REPO_PATH = "gjoe9955-netizen/entrenador2"
# CORRECCIÓN: Apuntar al archivo correcto que usa el bot
HISTORIAL_FILE = "historial.json" 

def obtener_resultados_recientes():
    """Consulta los resultados finalizados de LaLiga"""
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
    
    # 1. Obtener Historial actual desde GitHub
    url_gh = f"https://api.github.com/repos/{REPO_PATH}/contents/{HISTORIAL_FILE}"
    headers_gh = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    
    try:
        r_gh = requests.get(url_gh, headers=headers_gh)
        if r_gh.status_code != 200:
            print("❌ No se pudo obtener el historial de GitHub.")
            return
            
        file_data = r_gh.json()
        historial = json.loads(base64.b64decode(file_data['content']).decode('utf-8'))
        
        # 2. Obtener resultados reales
        partidos_api = obtener_resultados_recientes()
        cambio = False

        # 3. Comparar y Cruzar Datos
        for pick in historial:
            if pick.get("status") == "⏳ PENDIENTE":
                for match in partidos_api:
                    home_api = match['homeTeam']['name'].lower()
                    away_api = match['awayTeam']['name'].lower()
                    
                    # Verificamos si el partido del historial coincide con el de la API
                    if home_api in pick['partido'].lower() and away_api in pick['partido'].lower():
                        goles_l = match['score']['fullTime']['home']
                        goles_v = match['score']['fullTime']['away']
                        marcador_real = f"{goles_l}-{goles_v}"
                        resultado = match['score']['winner'] # 'HOME_TEAM', 'AWAY_TEAM', 'DRAW'

                        pick["marcador_real"] = marcador_real
                        
                        # CAMBIO 2: Lógica de validación corregida para picks sin apuesta
                        PICKS_VOID = ["no bet", "no apostar", "no apostar (sin valor)", "sin valor"]
                        if any(v in pick['pick'].lower() for v in PICKS_VOID):
                            pick["status"] = "➖ VOID"
                        elif (resultado == 'HOME_TEAM' and home_api in pick['pick'].lower()) or \
                             (resultado == 'AWAY_TEAM' and away_api in pick['pick'].lower()) or \
                             (resultado == 'DRAW' and "empate" in pick['pick'].lower()):
                            pick["status"] = "✅ WIN"
                        else:
                            pick["status"] = "❌ LOSS"
                        
                        cambio = True
                        print(f"✅ Auditado: {pick['partido']} -> {marcador_real}")

        # 4. Guardar si hubo cambios
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