"""Cálculos de cada pestaña del panel, siempre separando corridas por API de mediciones manuales."""

import json
from collections import Counter, defaultdict


def _pct(a, b):
    return round(100 * a / b) if b else None


def metricas(filas):
    validas = [r for r in filas if not r["error"]]
    cat = [r for r in validas if r["tipo"] != "Marca"]
    mar = [r for r in validas if r["tipo"] == "Marca"]
    con = [r for r in validas if r["menciona"]]
    pos = [r["posicion"] for r in cat if r["menciona"] and r["posicion"]]
    return {
        "categoria_n": len(cat), "marca_n": len(mar),
        "mencion_categoria": _pct(sum(r["menciona"] for r in cat), len(cat)),
        "primer_lugar": _pct(sum(1 for r in cat if r["posicion"] == 1 and r["menciona"]), len(cat)),
        "posicion_promedio": round(sum(pos) / len(pos), 1) if pos else None,
        "mencion_marca": _pct(sum(r["menciona"] for r in mar), len(mar)),
        "cita_sitio": _pct(sum(r["cita_sitio"] for r in con), len(con)),
        "confusiones": sum(r["posible_confusion"] for r in validas),
        "errores": len(filas) - len(validas),
    }


def _celda(r):
    if r["error"]:
        return "error"
    if r["posible_confusion"] and not r["menciona"]:
        return "confusión"
    if r["menciona"]:
        return f"#{r['posicion']}" if r["posicion"] else "Sí"
    return "No"


COLS = ("id, corrida_id, fecha, motor, modelo, prompt_id, prompt, tipo, marca, familia, idioma, intento, menciona,"
        " posicion, cita_sitio, posible_confusion, competidores, dominios, busquedas, error, notas")


def resumen(bd, metodo="API"):
    corridas = bd.q("SELECT * FROM corridas WHERE metodo=? ORDER BY id", (metodo,))
    ids = [c["id"] for c in corridas]
    if not ids:
        return {"metodo": metodo, "vacio": True, "corridas": []}
    marcas = ",".join("?" * len(ids))
    filas = bd.q(f"SELECT {COLS} FROM respuestas WHERE corrida_id IN ({marcas}) ORDER BY id", ids)
    por = defaultdict(list)
    for r in filas:
        por[r["corrida_id"]].append(r)
    corridas = [c for c in corridas if por.get(c["id"])]
    if not corridas:
        return {"metodo": metodo, "vacio": True, "corridas": []}
    motores = sorted({r["motor"] for r in filas})
    ultima = corridas[-1]["id"]

    pulso = []
    for c in corridas:
        fs = por[c["id"]]
        pulso.append({"corrida": c["id"], "inicio": c["inicio"], "origen": c["origen"], "nota": c["nota"],
                      "todos": metricas(fs),
                      "motores": {m: metricas([r for r in fs if r["motor"] == m]) for m in motores
                                  if any(r["motor"] == m for r in fs)}})

    fan = defaultdict(lambda: {"veces": 0, "motores": set(), "prompts": set(), "con": 0, "cita": 0})
    for f in bd.q(f"""SELECT lower(trim(f.busqueda)) b, f.motor, f.prompt_id, r.menciona, r.cita_sitio
                      FROM fanouts f JOIN respuestas r ON r.id=f.respuesta_id
                      WHERE f.corrida_id IN ({marcas})""", ids):
        x = fan[f["b"]]
        x["veces"] += 1
        x["motores"].add(f["motor"])
        x["prompts"].add(f["prompt_id"])
        x["con"] += f["menciona"]
        x["cita"] += f["cita_sitio"]
    fanouts = sorted(({"b": b, "veces": v["veces"], "motores": sorted(v["motores"]), "prompts": sorted(v["prompts"]),
                       "con": v["con"], "cita": v["cita"]} for b, v in fan.items()),
                     key=lambda x: (-len(x["motores"]), -x["veces"]))

    cat = [r for r in filas if r["tipo"] != "Marca" and not r["error"]]
    empresas, dominios, con_d, sin_d = Counter(), Counter(), Counter(), Counter()
    for r in cat:
        for c in json.loads(r["competidores"] or "[]"):
            empresas[c] += 1
        ds = set(json.loads(r["dominios"] or "[]"))
        dominios.update(ds)
        (con_d if r["menciona"] else sin_d).update(ds)
    empresas["Dicema"] = sum(r["menciona"] for r in cat)
    n_con = sum(1 for r in cat if r["menciona"])
    brujula = sorted(({"d": d, "con": con_d[d], "sin": sin_d[d]} for d in set(con_d) | set(sin_d)),
                     key=lambda x: -(x["con"] + x["sin"]))[:80]

    return {
        "metodo": metodo, "vacio": False, "motores": motores, "ultima": ultima,
        "corridas": [{"id": c["id"], "inicio": c["inicio"], "origen": c["origen"], "nota": c["nota"],
                      "n": len(por[c["id"]])} for c in corridas],
        "pulso": pulso, "fanouts": fanouts[:2000],
        "empresas": empresas.most_common(30), "dominios": dominios.most_common(30),
        "brujula": brujula, "n_con": n_con, "n_sin": len(cat) - n_con,
    }


def matriz(bd, corrida_id):
    filas = bd.q(f"SELECT {COLS} FROM respuestas WHERE corrida_id=? ORDER BY prompt_id, motor, intento",
                 (corrida_id,))
    celdas, prompts = {}, {}
    for r in filas:
        prompts[r["prompt_id"]] = {"texto": r["prompt"], "tipo": r["tipo"], "marca": r["marca"],
                                   "familia": r["familia"], "idioma": r["idioma"]}
        celdas.setdefault(r["prompt_id"], {}).setdefault(r["motor"], []).append(
            {"v": _celda(r), "id": r["id"]})
    return {"corrida": corrida_id, "prompts": prompts, "celdas": celdas,
            "motores": sorted({r["motor"] for r in filas})}


def comparar(bd, a, b):
    ma, mb = matriz(bd, a), matriz(bd, b)
    rango = lambda v: 0 if v in ("No", "confusión", "error", None) else (
        100 - int(v[1:]) if v.startswith("#") else 50)
    cambios, iguales = [], 0
    prompts = {**ma["prompts"], **mb["prompts"]}
    motores = sorted(set(ma["motores"]) | set(mb["motores"]))
    filas = []
    for pid in sorted(prompts, key=lambda x: (x.endswith("-EN"), x)):
        fila = {"id": pid, "texto": prompts[pid]["texto"], "tipo": prompts[pid]["tipo"], "celdas": {}}
        for m in motores:
            va = (ma["celdas"].get(pid, {}).get(m) or [{}])[0].get("v")
            vb = (mb["celdas"].get(pid, {}).get(m) or [{}])[0].get("v")
            fila["celdas"][m] = [va, vb]
            if va is None or vb is None:
                continue
            if rango(vb) > rango(va):
                cambios.append({"id": pid, "texto": prompts[pid]["texto"], "motor": m, "de": va, "a": vb, "sube": True})
            elif rango(vb) < rango(va):
                cambios.append({"id": pid, "texto": prompts[pid]["texto"], "motor": m, "de": va, "a": vb, "sube": False})
            else:
                iguales += 1
        filas.append(fila)

    def dominios_de(cid):
        return Counter(r["dominio"] for r in bd.q("SELECT DISTINCT respuesta_id, dominio FROM fuentes WHERE corrida_id=?",
                                                  (cid,)) if r["dominio"])
    da, db_ = dominios_de(a), dominios_de(b)
    return {"a": a, "b": b, "motores": motores, "filas": filas, "iguales": iguales,
            "suben": [c for c in cambios if c["sube"]], "bajan": [c for c in cambios if not c["sube"]],
            "nuevas": [(d, n) for d, n in (db_ - da).most_common(15)],
            "perdidas": [(d, n) for d, n in (da - db_).most_common(15)]}


def detalle(bd, rid):
    r = bd.q("SELECT * FROM respuestas WHERE id=?", (rid,))
    if not r:
        return None
    r = r[0]
    r.pop("crudo", None)
    r["fuentes"] = bd.q("SELECT url, titulo, dominio, citada FROM fuentes WHERE respuesta_id=?", (rid,))
    for k in ("competidores", "dominios", "busquedas"):
        r[k] = json.loads(r[k] or "[]")
    return r
