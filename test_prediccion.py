import json
import numpy as np
from scipy.stats import poisson

# ============================================================
# Lógica idéntica al bot V9 — úsalo para validar
# modelo_poisson.json antes de deployar a Railway.
# Nuevos parámetros respecto a V8:
#   - forma_local_pts / forma_visita_pts : puntos de forma (0-15)
#   - pos_local / pos_visita             : posición en tabla
#   - pts_tabla_local / pts_tabla_visita : puntos en tabla
#   - penalty_local / penalty_visita     : factor Serper (0.95 si hay bajas)
# ============================================================

# --- Dixon-Coles (mismo rho que bot.py) ---
DC_RHO = -0.13

def dixon_coles_tau(x, y, lh, la, rho=DC_RHO):
    if x == 0 and y == 0: return 1.0 - (lh * la * rho)
    if x == 1 and y == 0: return 1.0 + (la * rho)
    if x == 0 and y == 1: return 1.0 + (lh * rho)
    if x == 1 and y == 1: return 1.0 - rho
    return 1.0

# --- Factor H2H sede-corregido (mismo que bot.py) ---
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

# --- NUEVO: Factor forma reciente ---
def calcular_factor_forma(puntos_forma):
    """
    puntos_forma: suma de pts de los últimos 5 partidos (máx 15).
    Devuelve (factor_ataque, factor_defensa, texto)
    """
    MAX_AJUSTE = 0.10
    forma_norm = puntos_forma / 15.0

    if forma_norm > 0.67:
        intensidad = (forma_norm - 0.67) / 0.33
        ajuste = MAX_AJUSTE * intensidad
        return 1.0 + ajuste, 1.0 - ajuste, f"Forma 🔥 {puntos_forma}pts/15 (+{ajuste*100:.1f}% atk)"
    elif forma_norm < 0.33:
        intensidad = (0.33 - forma_norm) / 0.33
        ajuste = MAX_AJUSTE * intensidad
        return 1.0 - ajuste, 1.0 + ajuste, f"Forma ❄️ {puntos_forma}pts/15 (-{ajuste*100:.1f}% atk)"
    else:
        return 1.0, 1.0, f"Forma ➡️ {puntos_forma}pts/15 (neutro)"

# --- NUEVO: Factor tabla ---
def calcular_factor_tabla(pos_local, pos_visita, pts_local, pts_visita):
    MAX_AJUSTE = 0.06
    diff_pos = pos_visita - pos_local

    if abs(diff_pos) < 6:
        return 1.0, 1.0, f"Tabla ⚖️ Diferencia leve ({pos_local}° vs {pos_visita}°, {pts_local}pts vs {pts_visita}pts)"

    intensidad = min((abs(diff_pos) - 6) / 14, 1.0)
    ajuste = MAX_AJUSTE * intensidad

    if diff_pos > 0:
        return 1.0 + ajuste, 1.0 - ajuste, f"Tabla 📈 Local superior ({pos_local}° vs {pos_visita}°, +{ajuste*100:.1f}% lh)"
    else:
        return 1.0 - ajuste, 1.0 + ajuste, f"Tabla 📉 Visita superior ({pos_local}° vs {pos_visita}°, +{ajuste*100:.1f}% la)"

# --- Motor principal V9 ---
def calcular_probabilidades(lh, la):
    ph, pd, pa, over25 = 0.0, 0.0, 0.0, 0.0
    scores = {}

    for x in range(7):
        for y in range(7):
            p = poisson.pmf(x, lh) * poisson.pmf(y, la) * dixon_coles_tau(x, y, lh, la)
            if x > y:    ph += p
            elif x == y: pd += p
            else:         pa += p
            if x + y > 2.5: over25 += p
            if x <= 5 and y <= 5:
                scores[f"{x}-{y}"] = p

    scores_sorted = sorted(scores.items(), key=lambda k: k[1], reverse=True)
    return ph, pd, pa, over25, scores_sorted

# --- Test completo V9 ---
def test_motor(
    local, visitante,
    h2h_local_wins=0, h2h_away_wins=0, h2h_total=0,
    forma_local_pts=7, forma_visita_pts=7,
    pos_local=10, pos_visita=10,
    pts_tabla_local=30, pts_tabla_visita=30,
    penalty_local=1.0, penalty_visita=1.0,
    cuota_empate=3.5
):
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

    # --- Factores individuales ---
    factor_lh_h2h, factor_la_h2h, h2h_texto = calcular_factor_h2h(h2h_local_wins, h2h_away_wins, h2h_total)
    forma_local_atk, _, forma_local_txt   = calcular_factor_forma(forma_local_pts)
    forma_visita_atk, _, forma_visita_txt = calcular_factor_forma(forma_visita_pts)
    factor_lh_tabla, factor_la_tabla, tabla_texto = calcular_factor_tabla(pos_local, pos_visita, pts_tabla_local, pts_tabla_visita)

    # --- Lambdas ajustadas ---
    lh = lh_base * factor_lh_h2h * forma_local_atk  * factor_lh_tabla * penalty_local
    la = la_base * factor_la_h2h * forma_visita_atk * factor_la_tabla * penalty_visita

    # --- Penalización cuota empate ---
    empate_nota = ""
    edge_penalty = 1.0
    if cuota_empate < 3.0:
        edge_penalty = 0.80
        empate_nota = f"⚠️ Cuota empate baja ({cuota_empate:.2f}) → edge ×0.80"
    else:
        empate_nota = f"Cuota empate OK ({cuota_empate:.2f})"

    print(f"\n{'='*55}")
    print(f"  BOT V9 — TEST: {local} vs {visitante}")
    print(f"{'='*55}")
    print(f"\n📋 MODELO:")
    print(f"   Última actualización : {data.get('last_update', 'N/A')}")
    print(f"   Partidos con xG      : {data.get('meta', {}).get('partidos_con_xg', 'N/A')}")

    print(f"\n⚽ LAMBDAS:")
    print(f"   λH base = {lh_base:.3f}  →  ajustado = {lh:.3f}")
    print(f"   λA base = {la_base:.3f}  →  ajustado = {la:.3f}")
    print(f"\n🔧 FACTORES APLICADOS:")
    print(f"   {h2h_texto}")
    print(f"   Local  → {forma_local_txt}")
    print(f"   Visita → {forma_visita_txt}")
    print(f"   {tabla_texto}")
    serper_txt = ""
    if penalty_local < 1.0:  serper_txt += f" ⚠️ Bajas local ({penalty_local})"
    if penalty_visita < 1.0: serper_txt += f" ⚠️ Bajas visita ({penalty_visita})"
    if serper_txt: print(f"   Serper:{serper_txt}")
    print(f"   {empate_nota}")

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
    if edge_penalty < 1.0:
        print(f"\n   ⚠️  Edge efectivo reducido ×{edge_penalty} por cuota empate baja")

    print(f"\n🎯 TOP 5 MARCADORES (con Dixon-Coles):")
    for i, (score, prob) in enumerate(scores_dc[:5]):
        bar = "█" * int(prob * 200)
        print(f"   {i+1}. {score}  {prob*100:5.2f}%  {bar}")

    print(f"\n{'='*55}\n")


if __name__ == "__main__":
    # --- Ejemplo básico sin parámetros opcionales ---
    test_motor("Real Madrid CF", "FC Barcelona")

    # --- Ejemplo completo con todos los factores V9 ---
    # Escenario: Real Madrid (1°, 75pts, forma 🔥 12pts) vs Barcelona (3°, 65pts, forma ❄️ 4pts)
    # H2H: Real Madrid ganó 3 de 5 siendo local
    # Sin bajas detectadas, cuota empate 3.80
    test_motor(
        "Real Madrid CF", "FC Barcelona",
        h2h_local_wins=3, h2h_away_wins=1, h2h_total=5,
        forma_local_pts=12,
        forma_visita_pts=4,
        pos_local=1,   pos_visita=3,
        pts_tabla_local=75, pts_tabla_visita=65,
        penalty_local=1.0, penalty_visita=1.0,
        cuota_empate=3.80
    )

    # --- Ejemplo con bajas y cuota empate baja ---
    # Sevilla (12°) vs Athletic (6°), cuota empate baja, bajas en local
    test_motor(
        "Sevilla FC", "Athletic Club",
        h2h_local_wins=2, h2h_away_wins=2, h2h_total=4,
        forma_local_pts=5,
        forma_visita_pts=10,
        pos_local=12, pos_visita=6,
        pts_tabla_local=35, pts_tabla_visita=50,
        penalty_local=0.95,   # baja clave en Sevilla detectada por Serper
        penalty_visita=1.0,
        cuota_empate=2.80     # cuota baja → edge penalizado
    )
