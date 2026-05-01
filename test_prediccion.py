import json
import numpy as np
from scipy.stats import poisson

# ============================================================
# Lógica idéntica al bot V11 — úsalo para validar
# modelo_poisson.json antes de deployar a Railway.
#
# FIXES V11 aplicados:
#   FIX 1 — calcular_factor_elo: HOME_ELO_BONUS +50 pts al local
#   FIX 2 — calcular_factor_h2h: umbral reducido a 1 partido sede
#   FIX 3 — calcular_lambdas_base: stats neutras + HOME_ADVANTAGE_FACTOR
#            (elimina doble conteo de ventaja de campo)
# ============================================================

# ── CONSTANTES DE VENTAJA DE CAMPO (FIX V11) ────────────────
HOME_ADVANTAGE_FACTOR = 1.10   # Local marca ~10% más en LaLiga (histórico)
HOME_ELO_BONUS        = 50     # Ventaja de campo en puntos Elo (estándar FIFA)
DC_RHO                = -0.13  # Dixon-Coles rho

# --- Dixon-Coles: corrección para marcadores bajos ---
def dixon_coles_tau(x, y, lh, la, rho=DC_RHO):
    if x == 0 and y == 0: return 1.0 - (lh * la * rho)
    if x == 1 and y == 0: return 1.0 + (la * rho)
    if x == 0 and y == 1: return 1.0 + (lh * rho)
    if x == 1 and y == 1: return 1.0 - rho
    return 1.0


# ============================================================
# FIX 2 — H2H sede-corregido: umbral reducido a 1 partido
# En LaLiga solo hay 1 enfrentamiento por sede por temporada,
# así que el umbral de 3 casi siempre caía al modo genérico.
# ============================================================
def calcular_factor_h2h(home_wins, away_wins, total_partidos):
    # FIX: umbral 1 en vez de 3
    if total_partidos < 1:
        return 1.0, 1.0, "H2H: Insuficientes datos"

    tasa_local  = home_wins / total_partidos
    tasa_visita = away_wins / total_partidos
    MAX_AJUSTE  = 0.08

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


# --- Factor forma reciente ---
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


# --- Factor tabla ---
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


# ============================================================
# FIX 1 — calcular_factor_elo: añade HOME_ELO_BONUS al local
# Convierte diferencia Elo en factor multiplicador para λH/λA.
# Máximo ajuste: ±8% (~200 puntos de diferencia efectiva).
# ============================================================
def calcular_factor_elo(elo_local, elo_visita):
    MAX_AJUSTE = 0.08
    MAX_DIFF   = 200.0

    # FIX: bonus de campo aplicado SOLO aquí, no en las lambdas
    elo_local_ajustado = elo_local + HOME_ELO_BONUS

    diff       = elo_local_ajustado - elo_visita
    intensidad = max(-1.0, min(diff / MAX_DIFF, 1.0))
    ajuste     = MAX_AJUSTE * intensidad

    factor_lh = round(1.0 + ajuste, 4)
    factor_la = round(1.0 - ajuste, 4)

    if abs(ajuste) < 0.01:
        texto = f"Elo ⚖️ Equilibrado ({elo_local:.0f}+50 vs {elo_visita:.0f})"
    elif diff > 0:
        texto = f"Elo 📈 Local superior ({elo_local:.0f}+50 vs {elo_visita:.0f}, +{ajuste*100:.1f}% lh)"
    else:
        texto = f"Elo 📉 Visita superior ({elo_local:.0f}+50 vs {elo_visita:.0f}, +{abs(ajuste)*100:.1f}% la)"

    return factor_lh, factor_la, texto


# ============================================================
# FIX 3 — calcular_lambdas_base: stats neutras + HOME_ADVANTAGE_FACTOR
# Elimina doble conteo: antes avg['league_home'] y el Elo
# sumaban ventaja de campo dos veces. Ahora:
#   - HOME_ADVANTAGE_FACTOR da la ventaja de campo en lambda
#   - HOME_ELO_BONUS        da la ventaja de campo en Elo
#   - avg_neutro            es el promedio sin sesgo de sede
# ============================================================
def calcular_lambdas_base(l_s, v_s, avg):
    """
    Jerarquía de stats del JSON:
      1. 'att' / 'def'        → stats globales neutras (ideal)
      2. promedio att_h+att_a → si no hay globales
      3. fallback att_h/att_a → último recurso
    """
    # Ataque local neutro
    if 'att' in l_s:
        att_local = l_s['att']
    elif 'att_h' in l_s and 'att_a' in l_s:
        att_local = (l_s['att_h'] + l_s['att_a']) / 2
    else:
        att_local = l_s.get('att_h', 1.0)

    # Defensa local neutra
    if 'def' in l_s:
        def_local = l_s['def']
    elif 'def_h' in l_s and 'def_a' in l_s:
        def_local = (l_s['def_h'] + l_s['def_a']) / 2
    else:
        def_local = l_s.get('def_h', 1.0)

    # Ataque visitante neutro
    if 'att' in v_s:
        att_visita = v_s['att']
    elif 'att_h' in v_s and 'att_a' in v_s:
        att_visita = (v_s['att_h'] + v_s['att_a']) / 2
    else:
        att_visita = v_s.get('att_a', 1.0)

    # Defensa visitante neutra
    if 'def' in v_s:
        def_visita = v_s['def']
    elif 'def_h' in v_s and 'def_a' in v_s:
        def_visita = (v_s['def_h'] + v_s['def_a']) / 2
    else:
        def_visita = v_s.get('def_a', 1.0)

    # Promedio de liga neutro (sin sesgo de sede)
    if 'league_avg' in avg:
        avg_neutro = avg['league_avg']
    else:
        avg_neutro = (avg.get('league_home', 1.5) + avg.get('league_away', 1.2)) / 2

    # FIX: HOME_ADVANTAGE_FACTOR es la ÚNICA fuente de ventaja de campo en lambda
    lh_base = att_local  * def_visita * avg_neutro * HOME_ADVANTAGE_FACTOR
    la_base = att_visita * def_local  * avg_neutro

    return lh_base, la_base


# --- Motor principal V11 ---
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


# --- Test completo V11 ---
def test_motor(
    local, visitante,
    h2h_local_wins=0, h2h_away_wins=0, h2h_total=0,
    forma_local_pts=7, forma_visita_pts=7,
    pos_local=10, pos_visita=10,
    pts_tabla_local=30, pts_tabla_visita=30,
    elo_local=1500.0, elo_visita=1500.0,
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

    # --- PASO 1: Lambdas base corregidas (FIX 3) ---
    lh_base, la_base = calcular_lambdas_base(s_l, s_v, avg)

    # --- Factores individuales ---
    factor_lh_h2h, factor_la_h2h, h2h_texto = calcular_factor_h2h(h2h_local_wins, h2h_away_wins, h2h_total)
    forma_local_atk,  _, forma_local_txt     = calcular_factor_forma(forma_local_pts)
    forma_visita_atk, _, forma_visita_txt    = calcular_factor_forma(forma_visita_pts)
    factor_lh_tabla, factor_la_tabla, tabla_texto = calcular_factor_tabla(pos_local, pos_visita, pts_tabla_local, pts_tabla_visita)

    # --- FIX 1: Factor Elo con bonus de campo ---
    factor_lh_elo, factor_la_elo, elo_texto = calcular_factor_elo(elo_local, elo_visita)

    # --- Lambdas ajustadas (todos los factores combinados) ---
    lh = lh_base * factor_lh_h2h * forma_local_atk  * factor_lh_tabla * factor_lh_elo * penalty_local
    la = la_base * factor_la_h2h * forma_visita_atk * factor_la_tabla * factor_la_elo * penalty_visita

    # --- Penalización cuota empate ---
    if cuota_empate < 3.0:
        edge_penalty = 0.80
        empate_nota  = f"⚠️ Cuota empate baja ({cuota_empate:.2f}) → edge ×0.80"
    else:
        edge_penalty = 1.0
        empate_nota  = f"Cuota empate OK ({cuota_empate:.2f})"

    print(f"\n{'='*55}")
    print(f"  BOT V11 — TEST: {local} vs {visitante}")
    print(f"{'='*55}")
    print(f"\n📋 MODELO:")
    print(f"   Última actualización : {data.get('last_update', 'N/A')}")
    print(f"   Partidos con xG      : {data.get('meta', {}).get('partidos_con_xg', 'N/A')}")

    print(f"\n⚽ LAMBDAS:")
    print(f"   λH base = {lh_base:.3f}  →  ajustado = {lh:.3f}")
    print(f"   λA base = {la_base:.3f}  →  ajustado = {la:.3f}")
    print(f"\n🔧 FACTORES APLICADOS (V11):")
    print(f"   HOME_ADVANTAGE_FACTOR : ×{HOME_ADVANTAGE_FACTOR} (solo en lambda base)")
    print(f"   HOME_ELO_BONUS        : +{HOME_ELO_BONUS} pts Elo al local")
    print(f"   {h2h_texto}")
    print(f"   Local  → {forma_local_txt}")
    print(f"   Visita → {forma_visita_txt}")
    print(f"   {tabla_texto}")
    print(f"   {elo_texto}")
    serper_txt = ""
    if penalty_local  < 1.0: serper_txt += f" ⚠️ Bajas local ({penalty_local})"
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

    # --- Ejemplo completo con todos los factores V11 ---
    # Escenario: Real Madrid (1°, 75pts, forma 🔥 12pts) vs Barcelona (3°, 65pts, forma ❄️ 4pts)
    # H2H: Real Madrid ganó 3 de 5 siendo local (umbral ahora es 1, aplica con 1+ partido)
    # Elo estimado: Madrid 1600, Barça 1580
    test_motor(
        "Real Madrid CF", "FC Barcelona",
        h2h_local_wins=3, h2h_away_wins=1, h2h_total=5,
        forma_local_pts=12,
        forma_visita_pts=4,
        pos_local=1,   pos_visita=3,
        pts_tabla_local=75, pts_tabla_visita=65,
        elo_local=1600.0, elo_visita=1580.0,
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
        elo_local=1450.0, elo_visita=1510.0,
        penalty_local=0.95,   # baja clave en Sevilla detectada por Serper
        penalty_visita=1.0,
        cuota_empate=2.80     # cuota baja → edge penalizado
    )

    # --- Ejemplo del caso Girona vs Mallorca (caso real que reveló el bug) ---
    # Con los fixes, Mallorca (visita, mejor forma y H2H) debería tener mayor peso
    test_motor(
        "Girona FC", "RCD Mallorca",
        h2h_local_wins=0, h2h_away_wins=1, h2h_total=1,  # Mallorca domina H2H
        forma_local_pts=4,   # Girona fría
        forma_visita_pts=7,  # Mallorca mejor forma
        pos_local=15, pos_visita=17,
        pts_tabla_local=38, pts_tabla_visita=35,
        elo_local=1476.0, elo_visita=1450.0,
        penalty_local=0.93, penalty_visita=0.93,
        cuota_empate=3.44
    )
