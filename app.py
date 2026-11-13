#!/usr/bin/env python3
"""Panel 'Dicema en las IAs': servidor web ligero, sin dependencias externas.

Variables de entorno (en Easypanel → Environment):
    PANEL_PASSWORD       contraseña para entrar al panel (obligatoria)
    CLAVE_SECRETA        texto largo al azar para firmar la sesión (recomendada)
    OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, PERPLEXITY_API_KEY, XAI_API_KEY
    CORRIDA_DIARIA       hora de la corrida automática, hora de Guatemala (por defecto 06:15; "no" la apaga)
    DATA_DIR             carpeta de la base de datos (por defecto /data)
    PORT                 puerto (por defecto 8000)
    SIMULAR=1            modo demostración con respuestas de ejemplo, sin gastar en APIs
    COOKIE_SEGURA=0      solo para probar sin https
"""

import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import analisis
import corredor
import panel_datos
from db import BD
from motores import NOMBRES

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", "/data"))
PASSWORD = os.environ.get("PANEL_PASSWORD", "")
SECRETO = (os.environ.get("CLAVE_SECRETA") or secrets.token_hex(32)).encode()
COOKIE_SEGURA = os.environ.get("COOKIE_SEGURA", "1") != "0"
DURACION_SESION = 30 * 24 * 3600

bd = BD(DATA / "dicema_ia.db")
bd.sembrar_prompts(BASE / "prompts_iniciales.csv")
corre = corredor.Corredor(bd)
MOTOR_POR_NOMBRE = {v.lower(): k for k, v in NOMBRES.items()}


# ------------------------------------------------------------------ sesión
def firmar(ts):
    return hmac.new(SECRETO, str(ts).encode(), hashlib.sha256).hexdigest()


def sesion_valida(cookie):
    try:
        ts, firma = cookie.split(".", 1)
        return hmac.compare_digest(firma, firmar(ts)) and time.time() - int(ts) < DURACION_SESION
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ medición manual
def _num(v):
    try:
        return int(float(str(v).strip())) if str(v).strip() else None
    except ValueError:
        return None


def _fecha(v):
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(v[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return corredor.ahora()[:10]


def _lista(v):
    return [re.sub(r"\s*\(.*?\)\s*", "", x).strip() for x in (v or "").split(";") if x.strip()]


def guardar_manual(d):
    motor = MOTOR_POR_NOMBRE.get(str(d.get("motor", "")).strip().lower())
    pid = str(d.get("prompt_id", "")).strip()
    if not motor or not pid:
        return None, "Falta el motor o el ID de la pregunta."
    fecha = _fecha(d.get("fecha"))
    p = next((x for x in bd.prompts() if x["id"] == pid), None) or \
        {"id": pid, "texto": "", "tipo": "Marca" if pid.upper().startswith("M") else "Categoría",
         "marca": "", "familia": "", "intencion": "", "idioma": "EN" if pid.endswith("-EN") else "ES"}
    intento = _num(d.get("intento")) or 1
    cid = bd.corrida_manual_del_dia(fecha)
    if bd.q("SELECT 1 FROM respuestas WHERE corrida_id=? AND motor=? AND prompt_id=? AND intento=?",
            (cid, motor, pid, intento)):
        return None, f"Ya existe {NOMBRES[motor]} · {pid} · intento {intento} del {fecha}."
    si = lambda v: str(v or "").strip().lower() in ("sí", "si", "yes", "1", "true")
    notas = str(d.get("notas") or "").strip()
    busq = [b for b in _lista(d.get("busquedas")) if not b.lower().startswith("sugeridas")]
    dominios = _lista(d.get("fuentes"))
    an = {"menciona": si(d.get("menciona")), "posicion": _num(d.get("posicion")),
          "metodo_posicion": "manual" if _num(d.get("posicion")) else "",
          "cita_sitio": si(d.get("cita_sitio")),
          "posible_confusion": bool(re.search(r"confund|confusi", notas, re.I)) or si(d.get("confusion")),
          "competidores": _lista(d.get("competidores")), "dominios": dominios}
    resp = {"texto": "", "busquedas": busq, "fuentes": [{"url": x, "titulo": x} for x in dominios], "citas": [],
            "modelo": "app (manual)"}
    rid = bd.guardar_respuesta(cid, fecha + "T12:00:00", motor, p, intento, resp, an, notas=notas)
    return rid, None


def importar_manual(texto):
    filas = list(csv.reader(io.StringIO(texto.lstrip("﻿"))))
    if not filas:
        return 0, ["El archivo está vacío."]
    claves = ["fecha", "motor", "prompt_id", "intento", "menciona", "posicion", "cita_sitio", "competidores",
              "fuentes", "busquedas", "captura", "notas"]
    ok, avisos = 0, []
    for i, f in enumerate(filas[1:], start=2):
        if not any(c.strip() for c in f):
            continue
        d = dict(zip(claves, f + [""] * (12 - len(f))))
        rid, err = guardar_manual(d)
        if rid:
            ok += 1
        else:
            avisos.append(f"Fila {i}: {err}")
    return ok, avisos


def registro_csv(corrida_id):
    filas = bd.q("SELECT * FROM respuestas WHERE corrida_id=? ORDER BY motor, prompt_id, intento", (corrida_id,))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Fecha", "Motor", "ID prompt", "Intento", "¿Menciona a Dicema?", "Posición", "¿Cita dicema.com.gt?",
                "Competidores nombrados", "Fuentes citadas", "Búsquedas visibles", "Captura", "Notas"])
    for r in filas:
        nota = r["notas"] or f"API · {r['modelo']}"
        if r["error"]:
            nota += f" · ERROR: {r['error'][:150]}"
        if r["posible_confusion"]:
            nota += " · posible confusión con otra empresa"
        w.writerow([r["fecha"][:10], NOMBRES.get(r["motor"], r["motor"]), r["prompt_id"], r["intento"],
                    "" if r["error"] else ("Sí" if r["menciona"] else "No"), r["posicion"] or "",
                    "" if r["error"] else ("Sí" if r["cita_sitio"] else "No"),
                    "; ".join(json.loads(r["competidores"] or "[]")), "; ".join(json.loads(r["dominios"] or "[]")[:12]),
                    "; ".join(json.loads(r["busquedas"] or "[]")), "", nota])
    return "﻿" + buf.getvalue()


# ------------------------------------------------------------------ servidor
class Manejador(BaseHTTPRequestHandler):
    server_version = "DicemaIA/1.0"

    def log_message(self, fmt, *args):
        if "/api/estado" not in (args[0] if args else ""):
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    # -------------------------------------------------- respuestas
    def _enviar(self, codigo, cuerpo, tipo="application/json; charset=utf-8", extra=None):
        datos = cuerpo if isinstance(cuerpo, bytes) else (
            cuerpo.encode("utf-8") if isinstance(cuerpo, str) else json.dumps(cuerpo, ensure_ascii=False).encode())
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(datos)

    def _redir(self, a, cookie=None):
        self.send_response(303)
        self.send_header("Location", a)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _autenticado(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "dicema_sesion" in c and sesion_valida(c["dicema_sesion"].value)

    def _cuerpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 5_000_000:
            raise ValueError("Archivo demasiado grande.")
        return self.rfile.read(n).decode("utf-8") if n else ""

    def _pagina(self, nombre):
        return (BASE / "web" / nombre).read_text(encoding="utf-8")

    # -------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/salud":
            return self._enviar(200, {"ok": True})
        if u.path == "/login":
            return self._enviar(200, self._pagina("login.html"), "text/html; charset=utf-8")
        if u.path == "/salir":
            return self._redir("/login", "dicema_sesion=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax")
        if not self._autenticado():
            if u.path.startswith("/api/"):
                return self._enviar(401, {"error": "Sesión vencida"})
            return self._redir("/login")
        try:
            if u.path == "/":
                html = self._pagina("index.html").replace("__SIMULAR__", "true" if corre.simular else "false")
                return self._enviar(200, html, "text/html; charset=utf-8")
            if u.path == "/api/resumen":
                return self._enviar(200, panel_datos.resumen(bd, q.get("metodo", "API")))
            if u.path == "/api/matriz":
                return self._enviar(200, panel_datos.matriz(bd, int(q["corrida"])))
            if u.path == "/api/comparar":
                return self._enviar(200, panel_datos.comparar(bd, int(q["a"]), int(q["b"])))
            if u.path == "/api/respuesta":
                d = panel_datos.detalle(bd, int(q["id"]))
                return self._enviar(200 if d else 404, d or {"error": "No existe"})
            if u.path == "/api/estado":
                return self._enviar(200, corre.estado)
            if u.path == "/api/motores":
                return self._enviar(200, {"motores": corre.motores_disponibles(),
                                          "diaria": os.environ.get("CORRIDA_DIARIA", "06:15")})
            if u.path == "/api/prompts":
                return self._enviar(200, {"prompts": bd.prompts()})
            if u.path == "/api/manual/lista":
                filas = bd.q("""SELECT r.id, r.fecha, r.motor, r.prompt_id, r.intento, r.menciona, r.posicion,
                                r.cita_sitio, r.posible_confusion, r.notas FROM respuestas r
                                JOIN corridas c ON c.id=r.corrida_id WHERE c.metodo='Manual'
                                ORDER BY r.fecha DESC, r.id DESC LIMIT 300""")
                return self._enviar(200, {"filas": filas})
            if u.path == "/api/exportar":
                cid = int(q["corrida"])
                return self._enviar(200, registro_csv(cid), "text/csv; charset=utf-8",
                                    {"Content-Disposition": f'attachment; filename="registro_corrida_{cid}.csv"'})
            return self._enviar(404, {"error": "No encontrado"})
        except (KeyError, ValueError) as e:
            return self._enviar(400, {"error": f"Parámetro inválido: {e}"})

    # -------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/login":
            datos = parse_qs(self._cuerpo())
            clave = (datos.get("clave") or [""])[0]
            if PASSWORD and hmac.compare_digest(clave.encode(), PASSWORD.encode()):
                ts = str(int(time.time()))
                cookie = (f"dicema_sesion={ts}.{firmar(ts)}; Max-Age={DURACION_SESION}; Path=/; HttpOnly; "
                          f"SameSite=Lax" + ("; Secure" if COOKIE_SEGURA else ""))
                return self._redir("/", cookie)
            time.sleep(1.5)
            return self._redir("/login?error=1")
        if not self._autenticado():
            return self._enviar(401, {"error": "Sesión vencida"})
        if self.headers.get("X-Dicema") != "1":
            return self._enviar(403, {"error": "Solicitud no permitida"})
        try:
            if u.path == "/api/manual/importar":
                ok, avisos = importar_manual(self._cuerpo())
                return self._enviar(200, {"importadas": ok, "avisos": avisos[:50]})
            d = json.loads(self._cuerpo() or "{}")
            if u.path == "/api/correr":
                cid, err = corre.iniciar(d.get("prompts") or None, d.get("motores") or None,
                                         d.get("repeticiones", 1), "botón", (d.get("nota") or "")[:200])
                return self._enviar(409 if err else 200, {"error": err} if err else {"corrida": cid})
            if u.path == "/api/prompts":
                pid = re.sub(r"[^A-Za-z0-9\-_]", "", str(d.get("id", "")))[:20]
                texto = str(d.get("texto", "")).strip()[:500]
                if not pid or not texto:
                    return self._enviar(400, {"error": "Faltan el ID o el texto."})
                bd.guardar_prompt({"id": pid, "texto": texto, "tipo": d.get("tipo") or "Categoría",
                                   "marca": d.get("marca", ""), "familia": d.get("familia", ""),
                                   "intencion": d.get("intencion", ""), "idioma": d.get("idioma") or "ES",
                                   "activo": 1 if d.get("activo", True) else 0})
                return self._enviar(200, {"ok": True})
            if u.path == "/api/manual":
                rid, err = guardar_manual(d)
                return self._enviar(400 if err else 200, {"error": err} if err else {"id": rid})
            if u.path == "/api/manual/borrar":
                r = bd.q("SELECT r.id FROM respuestas r JOIN corridas c ON c.id=r.corrida_id "
                         "WHERE r.id=? AND c.metodo='Manual'", (int(d["id"]),))
                if not r:
                    return self._enviar(404, {"error": "Solo se pueden borrar registros manuales."})
                bd.borrar_respuesta(int(d["id"]))
                return self._enviar(200, {"ok": True})
            return self._enviar(404, {"error": "No encontrado"})
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            return self._enviar(400, {"error": f"Datos inválidos: {e}"})


def main():
    if not PASSWORD:
        sys.exit("Falta la variable PANEL_PASSWORD: el panel no arranca sin contraseña.")
    corre.programar()
    puerto = int(os.environ.get("PORT", "8000"))
    print(f"Panel Dicema IA en el puerto {puerto} · simulación={corre.simular} · "
          f"corrida diaria={os.environ.get('CORRIDA_DIARIA', '06:15')}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", puerto), Manejador).serve_forever()


if __name__ == "__main__":
    main()
