"""Análisis de una respuesta: ¿menciona a Dicema?, ¿en qué lugar?, ¿cita el sitio?, ¿a quién más nombra?"""

import re
import unicodedata
from urllib.parse import urlparse


def _sin_acentos(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn").lower()


def dominio(url_o_titulo):
    s = (url_o_titulo or "").strip()
    if not s:
        return ""
    if "://" not in s:
        # Gemini entrega el dominio en el título (p. ej. "dicema.com.gt").
        return s.lower().removeprefix("www.").split("/")[0]
    return urlparse(s).netloc.lower().removeprefix("www.")


def _encontrar(texto_norm, termino):
    t = re.escape(_sin_acentos(termino))
    m = re.search(r"(?<![a-z0-9])" + t + r"(?![a-z0-9])", texto_norm)
    return m.start() if m else -1


# Línea que abre un elemento numerado: "1. Dicema", "### 2. **Kemik**", "**3) Nils Pira**"
_NUMERADA = re.compile(r"^\s{0,3}(?:#{1,4}\s*)?(?:\*\*)?\s*(\d{1,2})\s*[\.\)](?:\s+(.*))?\s*$")


def _posicion_por_lista(texto, terminos):
    """Si la respuesta es una lista numerada de proveedores, devuelve el número del que menciona a la marca."""
    lineas = (texto or "").splitlines()
    entradas, actual = [], None
    for linea in lineas:
        m = _NUMERADA.match(linea)
        if m and len(m.group(2) or "") <= 90:
            actual = {"num": int(m.group(1)), "titulo": m.group(2) or "", "cuerpo": [linea]}
            entradas.append(actual)
        elif actual is not None and not actual["titulo"] and linea.strip():
            actual["titulo"] = linea.strip()  # "2." sola y el nombre en la línea siguiente
            actual["cuerpo"].append(linea)
        elif actual is not None:
            if linea.strip().startswith("#") and not _NUMERADA.match(linea):
                actual = None  # termina la lista en un nuevo encabezado
            else:
                actual["cuerpo"].append(linea)
    for e in entradas:
        titulo = _sin_acentos(e["titulo"])
        if any(_encontrar(titulo, t) >= 0 for t in terminos):
            return e["num"]
    for e in entradas:
        cuerpo = _sin_acentos(" ".join(e["cuerpo"][:4]))
        if any(_encontrar(cuerpo, t) >= 0 for t in terminos):
            return e["num"]
    return None


def analizar(resp, cfg_marca, competidores):
    texto = resp.get("texto", "")
    norm = _sin_acentos(texto)
    terminos = cfg_marca["terminos"]
    pos_marca = min([p for p in (_encontrar(norm, t) for t in terminos) if p >= 0], default=-1)
    menciona = pos_marca >= 0

    nombrados = []
    for c in competidores:
        p = _encontrar(norm, c)
        if p >= 0:
            nombrados.append((p, c))
    nombrados.sort()
    vistos, comp = set(), []
    for p, c in nombrados:
        if _sin_acentos(c) not in vistos:
            vistos.add(_sin_acentos(c))
            comp.append(c)

    posicion, metodo = None, ""
    if menciona:
        posicion = _posicion_por_lista(texto, terminos)
        if posicion is not None:
            metodo = "lista"
        elif comp:
            antes = {_sin_acentos(c) for p, c in nombrados if p < pos_marca}
            posicion, metodo = len(antes) + 1, "orden de mención"

    dominios_marca = [d.lower() for d in cfg_marca["dominios"]]
    todas = [f["url"] for f in resp.get("fuentes", [])] + [f.get("titulo", "") for f in resp.get("fuentes", [])]
    todas += resp.get("citas", [])
    doms = [dominio(u) for u in todas if u]
    cita_sitio = any(any(dm in d for dm in dominios_marca) for d in doms)
    cita_en_texto = any(dm in norm for dm in dominios_marca)

    senales = [_sin_acentos(s) for s in cfg_marca.get("senales_de_confusion", [])]
    confusion = menciona and (any(s in norm for s in senales) or any("dicema.mx" in d for d in doms))

    dominios_fuentes = []
    for f in resp.get("fuentes", []):
        d = dominio(f["url"])
        if "vertexaisearch" in d or not d:
            d = dominio(f.get("titulo", ""))
        if d and d not in dominios_fuentes:
            dominios_fuentes.append(d)

    if confusion and not (cita_sitio or cita_en_texto):
        # Habla de otra empresa con el mismo nombre: no cuenta como mención de Dicema Guatemala.
        menciona, posicion, metodo = False, None, ""

    return {
        "menciona": menciona,
        "posicion": posicion,
        "metodo_posicion": metodo,
        "cita_sitio": cita_sitio or cita_en_texto,
        "competidores": comp,
        "dominios": dominios_fuentes,
        "posible_confusion": confusion,
    }
