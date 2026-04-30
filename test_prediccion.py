import json
import numpy as np
from scipy.stats import poisson

# ============================================================
# Lógica idéntica al bot V8 — úsalo para validar
# modelo_poisson.json antes de deployar a Railway.
# ============================================================

# --- Dixon-Coles (mismo rho que bot.py) ---
DC_RHO = -0.13

def dixon_coles_tau(x, y, lh, la, rho=DC_RHO):
    if x == 0 and y == 0: return 1.0 - (lh * la * rho)
    if x == 1 and y == 0: return 1.0 + (la * rho)
    if x == 0 and y == 1: return 1.0 + (lh * rho)
    if x == 1 and y == 1: return 1.0 - rho
    return 1.0

# --- Factor H2H (mismo que bot.py) ---
def calcular_factor_h2h(home_wins, away_wins, total_partidos):
    if total_partidos < 3:
        return 1.0, 1.0, "H2H: Insuficientes datos"
    tasa_local = home_wins / total_partidos
    tasa_visita = away_wins / total_partidos
    MAX_AJUSTE = 0.08
    if tasa_local > 0.60:
        intensidad = min((tasa_local - 0.60) / 0.40, 1.0)
        ajuste = MAX_AJUSTE * intensidad
        return 1.0 + ajuste, 1.0 - ajuste, f"H2H 🏠 Dominio local ({tasa_local*100:.0f}%, +{ajuste*100:.1f}% lh)"
    elif tasa_visita > 0.60:
        intensidad = min((tasa_visita - 0.60) / 0.40, 1.0)
        ajuste = MAX_AJUSTE * intensidad
        return 1.0 - ajuste, 1.0 + ajuste, f"H2H 🚩 Dominio visita ({tasa_visita*100:.0f}%, +{ajuste*100:.1f}% la)"
    else:
        return 1.0, 1.0, f"H2H ⚖️ Equilibrado ({home_wins}L/{away_wins}V)"

# --- Motor principal V8 ---
def calcular_probabilidades(lh, la):
    """
    Matriz 7x7 + Dixon-Coles.
    Devuelve (prob_local, prob_empate, prob_visita, over25, marcadores_top)
    """
    ph, pd, pa, over25 = 0.0, 0.0, 0.0, 0.0
    scores = {}

    for x in range(7):
        for y in range(7):
            p = poisson.pmf(x, lh) * poisson.pmf(y, la) * dixon_coles_tau(x, y, lh, la)
            if x > y:   ph += p
            elif x == y: pd += p
            else:        pa += p
            if x + y > 2.5: over25 += p
            if x <= 5 and y <= 5:  # top marcadores hasta 5-5
                scores[f"{x}-{y}"] = p

    scores_sorted = sorted(scores.items(), key=lambda k: k[1], reverse=True)
    return ph, pd, pa, over25, scores_sorted

# --- Test completo ---
def test_motor(local, visitante, h2h_local_wins=0, h2h_away_wins=0, h2h_total=0):
    try:
        with open('modelo_poisson.json', 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        print("❌ Error: modelo_poisson.json no encontrado.")
        return

    try:
        stats = data['LaLiga']['teams']
        avg   = data['LaLiga']['averages']
        s_l   = stats[local]
        s_v   = stats[visitante]
    except KeyError as e:
        print(f"❌ Equipo {e} no encontrado en el modelo.")
        print(f"   Equipos disponibles: {', '.join(stats.keys())}")
        return

    # --- Lambdas base ---
    lh_base = s_l['att_h'] * s_v['def_a'] * avg['league_home']
    la_base = s_v['att_a'] * s_l['def_h'] * avg['league_away']

    # --- Ajuste H2H ---
    factor_lh, factor_la, h2h_texto = calcular_factor_h2h(h2h_local_wins, h2h_away_wins, h2h_total)
    lh = lh_base * factor_lh
    la = la_base * factor_la

    print(f"\n{'='*50}")
    print(f"  BOT V8 — TEST: {local} vs {visitante}")
    print(f"{'='*50}")
    print(f"\n📋 MODELO:")
    print(f"   Última actualización : {data.get('last_update', 'N/A')}")
    print(f"   Partidos con xG      : {data.get('meta', {}).get('partidos_con_xg', 'N/A')}")
    print(f"   Partidos sin xG      : {data.get('meta', {}).get('partidos_sin_xg', 'N/A')}")

    print(f"\n⚽ LAMBDAS:")
    print(f"   λH base = {lh_base:.3f}  →  ajustado = {lh:.3f}  (factor H2H: ×{factor_lh:.3f})")
    print(f"   λA base = {la_base:.3f}  →  ajustado = {la:.3f}  (factor H2H: ×{factor_la:.3f})")
    print(f"   {h2h_texto}")

    # --- Comparación: con vs sin Dixon-Coles ---
    ph_dc, pd_dc, pa_dc, over25_dc, scores_dc = calcular_probabilidades(lh, la)

    ph_raw, pd_raw, pa_raw, over25_raw = 0.0, 0.0, 0.0, 0.0
    for x in range(7):
        for y in range(7):
            p = poisson.pmf(x, lh) * poisson.pmf(y, la)
            if x > y:    ph_raw += p
            elif x == y: pd_raw += p
            else:         pa_raw += p
            if x + y > 2.5: over25_raw += p

    print(f"\n📊 PROBABILIDADES 1X2:")
    print(f"   {'':25} {'Poisson puro':>14} {'+ Dixon-Coles':>14} {'Δ':>8}")
    print(f"   {'─'*63}")
    print(f"   🏠 Victoria {local[:18]:18} {ph_raw*100:>13.1f}% {ph_dc*100:>13.1f}% {(ph_dc-ph_raw)*100:>+7.2f}%")
    print(f"   🤝 Empate {'':18}   {pd_raw*100:>13.1f}% {pd_dc*100:>13.1f}% {(pd_dc-pd_raw)*100:>+7.2f}%")
    print(f"   🚩 Victoria {visitante[:18]:18} {pa_raw*100:>13.1f}% {pa_dc*100:>13.1f}% {(pa_dc-pa_raw)*100:>+7.2f}%")
    print(f"   📈 Over 2.5 {'':17}   {over25_raw*100:>13.1f}% {over25_dc*100:>13.1f}% {(over25_dc-over25_raw)*100:>+7.2f}%")

    total_dc = ph_dc + pd_dc + pa_dc
    print(f"\n   ✅ Suma total (DC): {total_dc*100:.2f}%  {'← OK' if abs(total_dc - 1.0) < 0.01 else '← ⚠️ revisar rho'}")

    print(f"\n🎯 TOP 5 MARCADORES (con Dixon-Coles):")
    for i, (score, prob) in enumerate(scores_dc[:5]):
        bar = "█" * int(prob * 200)
        print(f"   {i+1}. {score}  {prob*100:5.2f}%  {bar}")

    print(f"\n{'='*50}\n")


if __name__ == "__main__":
    # --- Ejemplo sin H2H (bot usará factor 1.0) ---
    test_motor("Real Madrid CF", "FC Barcelona")

    # --- Ejemplo con H2H real (ingresar datos manualmente para test) ---
    # Si en los últimos 5 H2H: Real Madrid ganó 3, Barcelona 1, empates 1
    test_motor(
        "Real Madrid CF", "FC Barcelona",
        h2h_local_wins=3,
        h2h_away_wins=1,
        h2h_total=5
    )
