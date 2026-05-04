# kelly_ia.py
import os
import logging
import asyncio
import requests

GROQ_KEY   = os.getenv('GROQ_API_KEY')
GROQ_KEY_2 = os.getenv('GROQ_API_KEY_2')

# Umbral mínimo de edge para activar Kelly IA (evita requests innecesarias)
EDGE_MINIMO = 0.04  # 4% — ruido estadístico en LaLiga top-6

# Top 6 LaLiga — mercado muy eficiente, edge < 4% es ruido
TOP6_LALIGA = {"real madrid", "barcelona", "atletico", "atlético", "athletic", "real sociedad", "betis", "atletico madrid"}


async def _call_groq(prompt: str, api_key: str) -> str:
    url     = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 80
    }
    try:
        r = await asyncio.to_thread(requests.post, url, headers=headers, json=payload, timeout=15)
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        logging.error(f"[KellyIA] Error Groq: {e}")
        return ""


def _es_top6(nombre: str) -> bool:
    nombre_lower = nombre.lower()
    return any(t in nombre_lower for t in TOP6_LALIGA)


async def evaluar_kelly_ia(datos: dict) -> dict:
    """
    Ajusta el stake usando IA con reglas específicas de LaLiga/fútbol europeo.
    Usa GROQ_KEY_2 (paga) si está disponible, sino GROQ_KEY (gratuita).
    Retorna: {"stake_ajustado": float, "razon": str}
    """
    api_key = GROQ_KEY_2 if GROQ_KEY_2 else GROQ_KEY
    if not api_key:
        return {"stake_ajustado": datos['stake'], "razon": "Sin API Key"}

    edge_max = max(datos['edge_l'], datos['edge_e'], datos['edge_v'])

    # No gastar request si el edge no supera el umbral mínimo
    if edge_max < EDGE_MINIMO:
        return {"stake_ajustado": round(datos['stake'] * 0.5, 2), "razon": f"Edge {edge_max*100:.1f}% < umbral LaLiga 4%"}

    es_top6_local  = _es_top6(datos.get('local', ''))
    es_top6_visita = _es_top6(datos.get('visita', ''))
    es_top6_partido = es_top6_local or es_top6_visita

    prompt = f"""Eres gestor de bankroll experto en fútbol europeo (LaLiga española).

DATOS:
- Partido: {datos['local']} vs {datos['visita']}
- Edge: L {datos['edge_l']:.1f}% E {datos['edge_e']:.1f}% V {datos['edge_v']:.1f}%
- Kelly base: {datos['stake']}% | Pick: {datos['pick']} | Nivel: {datos['nivel']}
- Shin z={datos['shin_z']:.4f} | Confianza: {datos['shin_confianza']}
- Std cuotas local: {datos['std_l']:.3f} | visita: {datos['std_v']:.3f}
- Forma local: {datos['forma_l']} | Forma visita: {datos['forma_v']}
- Bajas: {datos['bajas']}
- λH={datos['lh']:.2f} | λA={datos['la']:.2f}
- Partido top-6 LaLiga: {'SÍ' if es_top6_partido else 'No'}

REGLAS LALIGA (aplica en orden):
1. Top-6 (RMA/BAR/ATM/ATH/RSO/BET) + Edge < 6% → stake x0.50 (mercado hiper-eficiente)
2. Shin z > 0.02 → stake x0.75 (info asimétrica detectada)
3. Std cuotas > 0.15 → stake x0.80 (mercado dividido, incertidumbre alta)
4. λH < 1.10 con pick local → stake x0.80 (ataque local débil, partido cerrado)
5. λA < 0.85 con pick visita → stake x0.85 (ataque visitante débil en LaLiga)
6. Bajas delantero titular detectadas en pick ofensivo → stake x0.90
7. Cuota pick < 1.60 → stake x0.60 (cuota muy corta, valor real cuestionable en fútbol europeo)
8. Edge > 12% + cuota > 2.50 → stake x1.20 (señal fuerte, máx 5%)
9. λH entre 1.4-2.0 y λA entre 0.9-1.3 → partido equilibrado, no penalizar
10. Empate LaLiga prior 28%: si pick empate y cuota < 3.20 → stake x0.70

RESPONDE SOLO EN ESTE FORMATO (sin texto extra):
STAKE_AJUSTADO: X.XX
RAZON: [máximo 12 palabras]"""

    respuesta = await _call_groq(prompt, api_key)
    stake_aj  = datos['stake']
    razon     = "Sin ajuste"

    for linea in (respuesta or "").splitlines():
        linea = linea.strip()
        if linea.startswith("STAKE_AJUSTADO:"):
            try:
                stake_aj = float(linea.split(":", 1)[1].strip())
            except:
                pass
        elif linea.startswith("RAZON:"):
            razon = linea.split(":", 1)[1].strip()

    stake_aj = round(min(max(stake_aj, 0.25), 5.0), 2)
    api_usada = "GROQ_2" if GROQ_KEY_2 else "GROQ"
    logging.info(f"[KellyIA] {api_usada} | {datos['local']} vs {datos['visita']} → {datos['stake']}% → {stake_aj}% | {razon}")

    return {"stake_ajustado": stake_aj, "razon": razon}
