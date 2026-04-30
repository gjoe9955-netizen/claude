import json
import numpy as np
from scipy.stats import poisson

def ajuste_dixon_coles(x, y, lh, la, rho=-0.15):
    """Ajuste de correlación para marcadores bajos."""
    if x == 0 and y == 0: return 1 - (lh * la * rho)
    if x == 0 and y == 1: return 1 + (lh * rho)
    if x == 1 and y == 0: return 1 + (la * rho)
    if x == 1 and y == 1: return 1 - rho
    return 1.0

def test_motor(local, visitante):
    # 1. Cargar el motor generado por trainer.py
    try:
        with open('modelo_poisson.json', 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        print("❌ Error: modelo_poisson.json no encontrado.")
        return

    # 2. Extraer datos (asegurando la estructura de LaLiga)
    try:
        stats = data['LaLiga']['teams']
        avg = data['LaLiga']['averages']
        
        s_l = stats[local]
        s_v = stats[visitante]
    except KeyError as e:
        print(f"❌ Error: El equipo {e} no existe en el modelo actual.")
        return

    # 3. Calcular Lambdas (Goles esperados)
    # Nota: El id_api se ignora automáticamente aquí al llamar solo a las llaves estadísticas
    lh = s_l['att_h'] * s_v['def_a'] * avg['league_home']
    la = s_v['att_a'] * s_l['def_h'] * avg['league_away']
    
    print(f"\n📊 --- TEST DE PREDICCIÓN: {local} vs {visitante} ---")
    print(f"⚽ Expectativa Local (Lambda H): {lh:.2f}")
    print(f"⚽ Expectativa Visita (Lambda A): {la:.2f}")
    # Opcional: Mostrar el ID detectado para verificar que el JSON es el nuevo
    if "id_api" in s_l:
        print(f"🆔 IDs API: Local({s_l['id_api']}) vs Visita({s_v['id_api']})")
    print("-" * 45)

    # 4. Calcular Probabilidades 1X2 con matriz 7x7
    ph, pd, pa = 0, 0, 0
    over25 = 0
    
    for x in range(7):
        for y in range(7):
            p = (poisson.pmf(x, lh) * poisson.pmf(y, la)) * ajuste_dixon_coles(x, y, lh, la)
            
            if x > y: ph += p
            elif x == y: pd += p
            else: pa += p
            
            if (x + y) > 2.5: over25 += p

    print(f"🏠 Victoria {local}: {ph:.2%}")
    print(f"🤝 Empate: {pd:.2%}")
    print(f"🚀 Victoria {visitante}: {pa:.2%}")
    print(f"📈 Over 2.5 Goles: {over25:.2%}")
    print("-" * 45)

    # 5. Marcadores más probables
    scores = []
    for x in range(4):
        for y in range(4):
            p = (poisson.pmf(x, lh) * poisson.pmf(y, la)) * ajuste_dixon_coles(x, y, lh, la)
            scores.append((f"{x}-{y}", p))
    
    scores.sort(key=lambda x: x[1], reverse=True)
    print("🎯 Top 3 Marcadores Probables:")
    for i in range(3):
        print(f"   {i+1}. {scores[i][0]} ({scores[i][1]:.2%})")

if __name__ == "__main__":
    # Asegúrate de usar los nombres exactos que genera tu nuevo trainer.py
    test_motor("Real Madrid CF", "FC Barcelona")
