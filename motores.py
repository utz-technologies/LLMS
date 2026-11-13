"""Llamadas a las APIs de cada motor y lectura de sus respuestas.

Cada motor devuelve un diccionario normalizado:
    {
      "texto":     respuesta completa,
      "busquedas": búsquedas internas (fan-outs) que el motor hizo,
      "fuentes":   [{"url", "titulo"}] páginas que el motor consultó,
      "citas":     [url] páginas que el motor citó dentro de la respuesta,
      "modelo":    modelo que respondió,
      "crudo":     JSON original, para poder volver a analizarlo después
    }

Solo usa la biblioteca estándar de Python: no hay que instalar nada.
"""

import json
import time
import urllib.error
import urllib.request


class ErrorMotor(Exception):
    pass


def _post(url, cuerpo, encabezados, tiempo_max):
    datos = json.dumps(cuerpo).encode("utf-8")
    enc = {"Content-Type": "application/json"}
    enc.update(encabezados)
    ultimo_error = None
    for intento in range(4):
        req = urllib.request.Request(url, data=datos, headers=enc, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=tiempo_max) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detalle = e.read().decode("utf-8", "replace")[:500]
            ultimo_error = f"HTTP {e.code}: {detalle}"
            # 429 (límite) y 5xx (falla del servidor): esperar y reintentar.
            if e.code == 429 or e.code >= 500:
                time.sleep(5 * (2 ** intento))
                continue
            raise ErrorMotor(ultimo_error)
        except (urllib.error.URLError, TimeoutError) as e:
            ultimo_error = str(e)
            time.sleep(5 * (2 ** intento))
    raise ErrorMotor(ultimo_error or "sin respuesta")


def _unicos(lista):
    vistos, salida = set(), []
    for x in lista:
        clave = json.dumps(x, sort_keys=True) if isinstance(x, dict) else x
        if x and clave not in vistos:
            vistos.add(clave)
            salida.append(x)
    return salida


def _recorrer(obj):
    """Recorre todo el JSON (por si un proveedor cambia la forma de su respuesta)."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _recorrer(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _recorrer(v)


def _leer_estilo_responses(crudo):
    """Lee respuestas con el formato 'Responses' (OpenAI, xAI y Perplexity Agent)."""
    texto, busquedas, fuentes, citas = [], [], [], []
    for item in crudo.get("output", []) or []:
        tipo = item.get("type", "")
        if tipo == "message":
            for parte in item.get("content", []) or []:
                if parte.get("type") in ("output_text", "text"):
                    texto.append(parte.get("text", ""))
                    for a in parte.get("annotations", []) or []:
                        if a.get("url"):
                            citas.append(a["url"])
                            fuentes.append({"url": a["url"], "titulo": a.get("title", "")})
        elif "search" in tipo:
            accion = item.get("action") or {}
            for q in [accion.get("query"), item.get("query")]:
                if isinstance(q, str):
                    busquedas.append(q)
            for lista in (accion.get("queries"), item.get("queries")):
                if isinstance(lista, list):
                    busquedas.extend(q for q in lista if isinstance(q, str))
            for lista in (accion.get("sources"), item.get("results"), item.get("sources")):
                for s in lista or []:
                    if isinstance(s, dict) and s.get("url"):
                        fuentes.append({"url": s["url"], "titulo": s.get("title", "")})
    # Lista de citas a nivel superior (algunos proveedores la devuelven aparte).
    for c in crudo.get("citations", []) or []:
        url = c if isinstance(c, str) else (c.get("url") if isinstance(c, dict) else None)
        if url:
            citas.append(url)
            fuentes.append({"url": url, "titulo": ""})
    if not texto and isinstance(crudo.get("output_text"), str):
        texto.append(crudo["output_text"])
    return "\n".join(texto), _unicos(busquedas), _unicos(fuentes), _unicos(citas)


# ---------------------------------------------------------------- ChatGPT (OpenAI)

def chatgpt(pregunta, cfg, clave, ubic, tiempo_max):
    cuerpo = {
        "model": cfg["modelo"],
        "input": pregunta,
        "tools": [{
            "type": "web_search",
            "user_location": {
                "type": "approximate", "country": ubic["pais"], "city": ubic["ciudad"],
                "region": ubic["region"], "timezone": ubic["zona_horaria"],
            },
        }],
        "include": ["web_search_call.action.sources"],
    }
    crudo = _post("https://api.openai.com/v1/responses", cuerpo,
                  {"Authorization": f"Bearer {clave}"}, tiempo_max)
    texto, busquedas, fuentes, citas = _leer_estilo_responses(crudo)
    return {"texto": texto, "busquedas": busquedas, "fuentes": fuentes, "citas": citas,
            "modelo": crudo.get("model", cfg["modelo"]), "crudo": crudo}


# ---------------------------------------------------------------- Claude (Anthropic)

def leer_claude(bloques_por_turno):
    texto, busquedas, fuentes, citas = [], [], [], []
    for bloques in bloques_por_turno:
        for b in bloques:
            t = b.get("type")
            if t == "text":
                texto.append(b.get("text", ""))
                for c in b.get("citations", []) or []:
                    if c.get("url"):
                        citas.append(c["url"])
            elif t == "server_tool_use" and b.get("name") == "web_search":
                q = (b.get("input") or {}).get("query")
                if q:
                    busquedas.append(q)
            elif t == "web_search_tool_result":
                contenido = b.get("content")
                if isinstance(contenido, list):
                    for r in contenido:
                        if r.get("url"):
                            fuentes.append({"url": r["url"], "titulo": r.get("title", "")})
    return "".join(texto), _unicos(busquedas), _unicos(fuentes), _unicos(citas)


def claude(pregunta, cfg, clave, ubic, tiempo_max):
    herramienta = {
        "type": "web_search_20250305", "name": "web_search",
        "max_uses": cfg.get("busquedas_max", 5),
        "user_location": {"type": "approximate", "city": ubic["ciudad"], "region": ubic["region"],
                          "country": ubic["pais"], "timezone": ubic["zona_horaria"]},
    }
    mensajes = [{"role": "user", "content": pregunta}]
    turnos, crudos = [], []
    for _ in range(4):  # "pause_turn": Claude pide continuar una búsqueda larga.
        crudo = _post("https://api.anthropic.com/v1/messages",
                      {"model": cfg["modelo"], "max_tokens": 4096, "messages": mensajes,
                       "tools": [herramienta]},
                      {"x-api-key": clave, "anthropic-version": "2023-06-01"}, tiempo_max)
        crudos.append(crudo)
        turnos.append(crudo.get("content", []))
        if crudo.get("stop_reason") != "pause_turn":
            break
        mensajes = mensajes + [{"role": "assistant", "content": crudo.get("content", [])}]
    texto, busquedas, fuentes, citas = leer_claude(turnos)
    return {"texto": texto, "busquedas": busquedas, "fuentes": fuentes, "citas": citas,
            "modelo": crudos[-1].get("model", cfg["modelo"]),
            "crudo": crudos if len(crudos) > 1 else crudos[0]}


# ---------------------------------------------------------------- Gemini (Google)

def leer_gemini(crudo):
    texto, busquedas, fuentes, citas = [], [], [], []
    for cand in crudo.get("candidates", []) or []:
        for parte in (cand.get("content") or {}).get("parts", []) or []:
            if parte.get("text"):
                texto.append(parte["text"])
        meta = cand.get("groundingMetadata") or {}
        busquedas.extend(meta.get("webSearchQueries", []) or [])
        trozos = meta.get("groundingChunks", []) or []
        for ch in trozos:
            web = ch.get("web") or {}
            if web.get("uri"):
                # Google entrega un enlace de redirección; el título suele ser el dominio real.
                fuentes.append({"url": web["uri"], "titulo": web.get("title", "")})
        usados = set()
        for s in meta.get("groundingSupports", []) or []:
            usados.update(s.get("groundingChunkIndices", []) or [])
        for i in sorted(usados):
            if i < len(trozos):
                web = trozos[i].get("web") or {}
                citas.append(web.get("title") or web.get("uri", ""))
    return "".join(texto), _unicos(busquedas), _unicos(fuentes), _unicos(citas)


def gemini(pregunta, cfg, clave, ubic, tiempo_max):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{cfg['modelo']}:generateContent"
    crudo = _post(url, {"contents": [{"parts": [{"text": pregunta}]}], "tools": [{"google_search": {}}]},
                  {"x-goog-api-key": clave}, tiempo_max)
    texto, busquedas, fuentes, citas = leer_gemini(crudo)
    return {"texto": texto, "busquedas": busquedas, "fuentes": fuentes, "citas": citas,
            "modelo": crudo.get("modelVersion", cfg["modelo"]), "crudo": crudo}


# ---------------------------------------------------------------- Perplexity (Agent API)

def perplexity(pregunta, cfg, clave, ubic, tiempo_max):
    cuerpo = {
        "effort": cfg.get("esfuerzo", "fast"),
        "input": pregunta,
        "tools": [{"type": "web_search",
                   "user_location": {"country": ubic["pais"], "city": ubic["ciudad"],
                                     "region": ubic["region"]}}],
    }
    if cfg.get("modelo"):
        cuerpo["model"] = cfg["modelo"]
    crudo = _post("https://api.perplexity.ai/v1/agent", cuerpo,
                  {"Authorization": f"Bearer {clave}"}, tiempo_max)
    texto, busquedas, fuentes, citas = _leer_estilo_responses(crudo)
    return {"texto": texto, "busquedas": busquedas, "fuentes": fuentes, "citas": citas,
            "modelo": crudo.get("model", "perplexity-" + cfg.get("esfuerzo", "fast")), "crudo": crudo}


# ---------------------------------------------------------------- Grok (xAI)

def grok(pregunta, cfg, clave, ubic, tiempo_max):
    cuerpo = {"model": cfg["modelo"], "input": [{"role": "user", "content": pregunta}],
              "tools": [{"type": "web_search"}]}
    crudo = _post("https://api.x.ai/v1/responses", cuerpo,
                  {"Authorization": f"Bearer {clave}"}, tiempo_max)
    texto, busquedas, fuentes, citas = _leer_estilo_responses(crudo)
    return {"texto": texto, "busquedas": busquedas, "fuentes": fuentes, "citas": citas,
            "modelo": crudo.get("model", cfg["modelo"]), "crudo": crudo}


MOTORES = {"chatgpt": chatgpt, "claude": claude, "gemini": gemini,
           "perplexity": perplexity, "grok": grok}

NOMBRES = {"chatgpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini",
           "perplexity": "Perplexity", "grok": "Grok", "copilot": "Copilot"}


def releer(motor, crudo):
    """Vuelve a leer una respuesta guardada (sirve si mejoramos el análisis después)."""
    if motor == "claude":
        partes = crudo if isinstance(crudo, list) else [crudo]
        return leer_claude([p.get("content", []) for p in partes])
    if motor == "gemini":
        return leer_gemini(crudo)
    return _leer_estilo_responses(crudo)
