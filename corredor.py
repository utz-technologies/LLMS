"""Corridas por API en segundo plano y corrida automática diaria."""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import analisis
import motores

BASE = Path(__file__).resolve().parent
# Guatemala no usa horario de verano: UTC-6 todo el año.
TZ = timezone(timedelta(hours=int(os.environ.get("UTC_OFFSET", "-6"))))


def ahora():
    return datetime.now(TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def cargar_config():
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    for m, c in cfg["motores"].items():
        sobre = os.environ.get(f"MODELO_{m.upper()}")
        if sobre:
            c["modelo"] = sobre
    return cfg


def _simulado(nombre):
    crudo = json.loads((BASE / "pruebas" / f"{nombre}.json").read_text(encoding="utf-8"))

    def llamar(pregunta, cfg, clave, ubic, tiempo_max):
        time.sleep(0.05)
        texto, busquedas, fuentes, citas = motores.releer(nombre, crudo)
        return {"texto": texto, "busquedas": busquedas, "fuentes": fuentes, "citas": citas,
                "modelo": "simulado", "crudo": crudo}
    return llamar


class Corredor:
    def __init__(self, bd):
        self.bd = bd
        self.simular = os.environ.get("SIMULAR", "").lower() in ("1", "true", "si", "sí")
        self.candado = threading.Lock()
        self.estado = {"corriendo": False, "corrida": None, "hechas": 0, "total": 0, "log": [], "inicio": None,
                       "motores": [], "vivo": {}}

    # -------------------------------------------------------------- motores disponibles
    def motores_disponibles(self):
        cfg = cargar_config()
        salida = []
        for m, c in cfg["motores"].items():
            clave = os.environ.get(c["variable_clave"], "")
            salida.append({"id": m, "nombre": motores.NOMBRES[m], "activo": bool(c.get("activo")),
                           "con_clave": bool(clave) or self.simular, "modelo": c.get("modelo") or c.get("esfuerzo")})
        return salida

    def _log(self, linea):
        self.estado["log"].append(f"{ahora()[11:19]}  {linea}")
        self.estado["log"] = self.estado["log"][-400:]

    # -------------------------------------------------------------- corrida
    def iniciar(self, ids_prompts=None, ids_motores=None, repeticiones=1, origen="botón", nota="", libre=""):
        if not self.candado.acquire(blocking=False):
            return None, "Ya hay una corrida en curso."
        try:
            cfg = cargar_config()
            prompts = self.bd.prompts(solo_activos=not ids_prompts)
            libre = (libre or "").strip()[:500]
            if libre:
                # Pregunta libre: se guarda aparte (tipo "Libre") y no entra en las métricas.
                pid = "LIBRE-" + ahora().replace("-", "").replace(":", "").replace("T", "")[2:12]
                self.bd.guardar_prompt({"id": pid, "texto": libre, "tipo": "Libre", "marca": "", "familia": "",
                                        "intencion": "Pregunta libre", "idioma": "ES", "activo": 0})
                prompts = [p for p in self.bd.prompts() if p["id"] == pid]
            elif ids_prompts:
                pedidos = set(ids_prompts)
                prompts = [p for p in prompts if p["id"] in pedidos]
            disponibles = {m["id"]: m for m in self.motores_disponibles()}
            ms = ids_motores or [m for m, d in disponibles.items() if d["activo"]]
            ms = [m for m in ms if disponibles.get(m, {}).get("con_clave")]
            if not prompts or not ms:
                self.candado.release()
                return None, "No hay preguntas activas o ningún motor tiene clave de API."
            repeticiones = max(1, min(int(repeticiones or 1), 5))
            total = len(prompts) * len(ms) * repeticiones
            origen = "simulación" if self.simular else origen
            cid = self.bd.nueva_corrida(ahora(), "API", origen, nota, total)
            self.estado.update({"corriendo": True, "corrida": cid, "hechas": 0, "total": total, "log": [],
                                "inicio": ahora(), "motores": ms,
                                "vivo": {m: {"estado": "en espera", "actual": "", "items": []} for m in ms}})
            self._log(f"Corrida {cid}: {len(prompts)} pregunta(s) × {len(ms)} motor(es) × {repeticiones}")
            hilo = threading.Thread(target=self._correr, args=(cid, cfg, prompts, ms, repeticiones), daemon=True)
            hilo.start()
            return cid, None
        except Exception:
            self.candado.release()
            raise

    def _correr(self, cid, cfg, prompts, ms, repeticiones):
        try:
            hilos = [threading.Thread(target=self._motor, args=(cid, cfg, m, prompts, repeticiones)) for m in ms]
            for h in hilos:
                h.start()
            for h in hilos:
                h.join()
            self.bd.cerrar_corrida(cid, ahora())
            for v in self.estado.get("vivo", {}).values():
                v.update({"estado": "listo", "actual": ""})
            self._log(f"Corrida {cid} terminada.")
        finally:
            self.estado["corriendo"] = False
            self.candado.release()

    def _motor(self, cid, cfg, m, prompts, repeticiones):
        mcfg = cfg["motores"][m]
        clave = os.environ.get(mcfg["variable_clave"], "")
        funcion = _simulado(m) if self.simular else motores.MOTORES[m]
        vivo = self.estado["vivo"][m]
        for p in prompts:
            for intento in range(1, repeticiones + 1):
                vivo.update({"estado": "pensando", "actual": p["id"]})
                error, resp, an = "", {"texto": "", "busquedas": [], "fuentes": [], "citas": [], "modelo": ""}, {}
                try:
                    resp = funcion(p["texto"], mcfg, clave, cfg["ubicacion"], cfg["tiempo_maximo_por_pregunta_segundos"])
                    an = analisis.analizar(resp, cfg["marca"], cfg["competidores"])
                except Exception as e:  # noqa: BLE001 (se registra y se sigue)
                    error = str(e)[:500]
                rid = self.bd.guardar_respuesta(cid, ahora(), m, p, intento, resp, an, error=error,
                                                notas="", crudo=resp.get("crudo"))
                self.estado["hechas"] += 1
                celda = ("error" if error else "confusión" if an.get("posible_confusion") and not an.get("menciona")
                         else (f"#{an['posicion']}" if an.get("posicion") else "Sí") if an.get("menciona") else "No")
                vivo["items"].append({"rid": rid, "pid": p["id"], "intento": intento, "v": celda,
                                      "texto": (resp.get("texto") or "")[:1500], "busquedas": resp.get("busquedas", [])[:8],
                                      "error": error[:200]})
                vivo["items"] = vivo["items"][-60:]
                if error:
                    self._log(f"{motores.NOMBRES[m]} · {p['id']} · ERROR {error[:120]}")
                else:
                    pos = f" #{an['posicion']}" if an.get("posicion") else ""
                    self._log(f"{motores.NOMBRES[m]} · {p['id']} · " +
                              (f"Dicema{pos}" if an.get("menciona") else
                               "confusión con otra empresa" if an.get("posible_confusion") else "sin Dicema"))
                if not self.simular:
                    time.sleep(cfg.get("pausa_entre_preguntas_segundos", 2))

    # -------------------------------------------------------------- programación diaria
    def programar(self):
        hora = os.environ.get("CORRIDA_DIARIA", "06:15").strip()
        if not hora or hora.lower() in ("no", "off", "0"):
            return
        hh, mm = (int(x) for x in hora.split(":"))

        def ciclo():
            ultima_fecha = None
            while True:
                t = datetime.now(TZ)
                if (t.hour, t.minute) == (hh, mm) and ultima_fecha != t.date():
                    ultima_fecha = t.date()
                    cid, err = self.iniciar(origen="automática")
                    print(f"[programada] corrida {cid} {err or ''}", flush=True)
                time.sleep(20)
        threading.Thread(target=ciclo, daemon=True).start()
