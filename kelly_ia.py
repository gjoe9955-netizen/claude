# kelly_ia.py
async def evaluar_kelly_ia(ejecutar_ia_fn, datos: dict) -> dict:
    prompt = f"""
Eres gestor de bankroll especializado en LaLiga española.
Datos del partido:
- Edge calculado: L {datos['edge_l']:.1f}% E {datos['edge_e']:.1f}% V {datos['edge_v']:.1f}%
- Kelly base: {datos['stake']}% | Nivel: {datos['nivel']}
- Shin z={datos['shin_z']:.4f} | Confianza: {datos['shin_confianza']}
- Std cuotas local: {datos['std_l']:.3f} | visita: {datos['std_v']:.3f}
- Equipos: {datos['local']} vs {datos['visita']}
- Forma: {datos['forma_l']} / {datos['forma_v']}
- Bajas: {datos['bajas']}

Reglas LaLiga:
- Edge < 4% en top-6 = ruido, recomienda reducir stake 50%
- Shin z > 0.02 = info asimétrica, reducir 25%
- Std cuotas > 0.15 = mercado dividido, reducir 20%
- Empate LaLiga tiene prior alto (28%), penalizar picks locales con λH < 1.2

Responde SOLO en este formato:
STAKE_AJUSTADO: X.XX
RAZON: [máximo 20 palabras]
"""
    respuesta = await ejecutar_ia_fn("estratega", prompt)
    # parsear respuesta
    stake_aj = datos['stake']
    razon = ""
    for linea in (respuesta or "").splitlines():
        if linea.startswith("STAKE_AJUSTADO:"):
            try: stake_aj = float(linea.split(":")[1].strip())
            except: pass
        if linea.startswith("RAZON:"):
            razon = linea.split(":", 1)[1].strip()
    return {"stake_ajustado": round(min(max(stake_aj, 0.25), 5.0), 2), "razon": razon}
