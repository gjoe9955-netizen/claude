# kelly_ia.py
import os
import logging
import requests
import asyncio

GROQ_KEY   = os.getenv('GROQ_API_KEY')
GROQ_KEY_2 = os.getenv('GROQ_API_KEY_2')  # API paga

async def _call_groq(prompt: str, api_key: str) -> str:
    url     = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1
    }
    try:
        r = await asyncio.to_thread(requests.post, url, headers=headers, json=payload, timeout=15)
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        logging.error(f"[KellyIA] Error Groq: {e}")
        return ""

async def evaluar_kelly_ia(datos: dict) -> dict:
    """
    Usa GROQ_KEY_2 (paga) si está disponible, sino GROQ_KEY (gratuita).
    Retorna stake_ajustado y razon para inyectar en el prompt final.
    """
    api_key = GROQ_KEY_2 if GROQ_KEY_2 else GROQ_KEY
    if not api_key:
        return {"stake_ajustado": datos['stake'], "razon": "Sin API Key"}

    # Solo activar si edge supera umbral mínimo (evita requests innecesarias)
    if max(datos['edge_l'], datos['edge_e'], datos['edge_v']) < 0.03:
        return {"stake_ajustado": datos['stake'], "razon": "Edge insuficiente, Kelly sin ajuste"}

    prompt = f"""
Eres gestor de bankroll especializado en LaLiga española.

DATOS:
- Partido: {datos['local']} vs {datos['visita']}
- Edge: L {datos['edge_l']:.1f}% E {datos['edge_e']:.1f}% V {datos['edge_v']:.1f}%
- Kelly base: {datos['stake']}% | Pick: {datos['pick']} | Nivel: {datos['nivel']}
- Shin z={datos['shin_z']:.4f} | Confianza: {datos['shin_confianza']}
- Std cuotas local: {datos['std_l']:.3f} | visita: {datos['std_v']:.3f}
- Forma: {datos['forma_l']} / {datos['forma_v']}
- Bajas detectadas: {datos['bajas']}
- λH={datos['lh']:.2f} λA={datos['la']:.2f}

REGLAS LALIGA:
- Edge < 4% en top-6 (RMA/BAR/ATM/ATH/RSO/BET) = ruido → stake x0.5
- Shin z > 0.02 = info asimétrica → stake x0.75
- Std cuotas > 0.15 = mercado dividido → stake x0.80
- λH < 1.2 con pick local = partido cerrado → stake x0.85
- Bajas delantero titular = penalizar pick ofensivo x0.90

RESPONDE SOLO ASÍ (sin texto extra):
STAKE_AJUSTADO: X.XX
RAZON: [máximo 15 palabras]
"""

    respuesta = await _call_groq(prompt, api_key)
    stake_aj  = datos['stake']
    razon     = "Sin ajuste"

    for linea in (respuesta or "").splitlines():
        if linea.startswith("STAKE_AJUSTADO:"):
            try:
                stake_aj = float(linea.split(":")[1].strip())
            except:
                pass
        if linea.startswith("RAZON:"):
            razon = linea.split(":", 1)[1].strip()

    stake_aj = round(min(max(stake_aj, 0.25), 5.0), 2)
    api_usada = "GROQ_2" if GROQ_KEY_2 else "GROQ"
    logging.info(f"[KellyIA] {api_usada} → stake {datos['stake']}% → {stake_aj}% | {razon}")

    return {"stake_ajustado": stake_aj, "razon": razon}
