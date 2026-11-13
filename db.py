"""Base de datos SQLite: preguntas, corridas, respuestas, fan-outs y fuentes."""

import csv
import json
import sqlite3
import threading
from pathlib import Path

from analisis import dominio

ESQUEMA = """
CREATE TABLE IF NOT EXISTS prompts (
    id TEXT PRIMARY KEY, texto TEXT NOT NULL, tipo TEXT, marca TEXT, familia TEXT, intencion TEXT,
    idioma TEXT, activo INTEGER DEFAULT 1, creado TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS corridas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, inicio TEXT, fin TEXT,
    metodo TEXT DEFAULT 'API', origen TEXT, nota TEXT, total INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS respuestas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    corrida_id INTEGER, fecha TEXT, motor TEXT, modelo TEXT,
    prompt_id TEXT, prompt TEXT, tipo TEXT, marca TEXT, familia TEXT, intencion TEXT, idioma TEXT,
    intento INTEGER, texto TEXT, menciona INTEGER, posicion INTEGER, metodo_posicion TEXT,
    cita_sitio INTEGER, posible_confusion INTEGER, competidores TEXT, dominios TEXT,
    busquedas TEXT, error TEXT, notas TEXT, crudo TEXT
);
CREATE TABLE IF NOT EXISTS fanouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, respuesta_id INTEGER, corrida_id INTEGER,
    motor TEXT, prompt_id TEXT, busqueda TEXT
);
CREATE TABLE IF NOT EXISTS fuentes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, respuesta_id INTEGER, corrida_id INTEGER,
    motor TEXT, prompt_id TEXT, url TEXT, titulo TEXT, dominio TEXT, citada INTEGER
);
CREATE INDEX IF NOT EXISTS ix_resp_corrida ON respuestas(corrida_id);
CREATE INDEX IF NOT EXISTS ix_fan_resp ON fanouts(respuesta_id);
CREATE INDEX IF NOT EXISTS ix_fue_resp ON fuentes(respuesta_id);
"""


class BD:
    def __init__(self, ruta):
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(ruta, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(ESQUEMA)
        self.candado = threading.RLock()

    def q(self, sql, args=()):
        with self.candado:
            return [dict(r) for r in self.con.execute(sql, args).fetchall()]

    def ex(self, sql, args=()):
        with self.candado:
            cur = self.con.execute(sql, args)
            self.con.commit()
            return cur.lastrowid

    # ------------------------------------------------------------ preguntas
    def sembrar_prompts(self, ruta_csv):
        if self.q("SELECT COUNT(*) n FROM prompts")[0]["n"]:
            return
        with open(ruta_csv, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                self.guardar_prompt({"id": r["ID"], "texto": r["Prompt"], "tipo": r["Tipo"], "marca": r["Marca"],
                                     "familia": r["Familia"], "intencion": r["Intención"], "idioma": r["Idioma"],
                                     "activo": 1})

    def guardar_prompt(self, p):
        self.ex("""INSERT INTO prompts (id, texto, tipo, marca, familia, intencion, idioma, activo)
                   VALUES (:id,:texto,:tipo,:marca,:familia,:intencion,:idioma,:activo)
                   ON CONFLICT(id) DO UPDATE SET texto=excluded.texto, tipo=excluded.tipo, marca=excluded.marca,
                   familia=excluded.familia, intencion=excluded.intencion, idioma=excluded.idioma,
                   activo=excluded.activo""", p)

    def prompts(self, solo_activos=False):
        sql = "SELECT * FROM prompts" + (" WHERE activo=1" if solo_activos else "")
        return self.q(sql + " ORDER BY (idioma='EN'), id")

    # ------------------------------------------------------------ corridas
    def nueva_corrida(self, inicio, metodo, origen, nota, total):
        return self.ex("INSERT INTO corridas (inicio, metodo, origen, nota, total) VALUES (?,?,?,?,?)",
                       (inicio, metodo, origen, nota, total))

    def cerrar_corrida(self, cid, fin):
        self.ex("UPDATE corridas SET fin=? WHERE id=?", (fin, cid))

    def corrida_manual_del_dia(self, fecha):
        filas = self.q("SELECT id FROM corridas WHERE metodo='Manual' AND substr(inicio,1,10)=?", (fecha,))
        if filas:
            return filas[0]["id"]
        return self.nueva_corrida(fecha + "T12:00:00", "Manual", "manual", "", 0)

    # ------------------------------------------------------------ respuestas
    def guardar_respuesta(self, cid, fecha, motor, p, intento, resp, an, error="", notas="", crudo=None):
        with self.candado:
            cur = self.con.execute(
                """INSERT INTO respuestas (corrida_id, fecha, motor, modelo, prompt_id, prompt, tipo, marca, familia,
                   intencion, idioma, intento, texto, menciona, posicion, metodo_posicion, cita_sitio,
                   posible_confusion, competidores, dominios, busquedas, error, notas, crudo)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, fecha, motor, resp.get("modelo", ""), p["id"], p["texto"], p.get("tipo", ""), p.get("marca", ""),
                 p.get("familia", ""), p.get("intencion", ""), p.get("idioma", ""), intento, resp.get("texto", ""),
                 int(bool(an.get("menciona"))), an.get("posicion"), an.get("metodo_posicion", ""),
                 int(bool(an.get("cita_sitio"))), int(bool(an.get("posible_confusion"))),
                 json.dumps(an.get("competidores", []), ensure_ascii=False),
                 json.dumps(an.get("dominios", []), ensure_ascii=False),
                 json.dumps(resp.get("busquedas", []), ensure_ascii=False), error, notas,
                 json.dumps(crudo, ensure_ascii=False) if crudo is not None else None))
            rid = cur.lastrowid
            for b in resp.get("busquedas", []):
                if b and b.strip():
                    self.con.execute("INSERT INTO fanouts (respuesta_id, corrida_id, motor, prompt_id, busqueda)"
                                     " VALUES (?,?,?,?,?)", (rid, cid, motor, p["id"], b.strip()))
            citadas = set(resp.get("citas", []))
            for f in resp.get("fuentes", []):
                d = dominio(f.get("url", ""))
                if "vertexaisearch" in d or not d:
                    d = dominio(f.get("titulo", ""))
                self.con.execute("INSERT INTO fuentes (respuesta_id, corrida_id, motor, prompt_id, url, titulo, dominio,"
                                 " citada) VALUES (?,?,?,?,?,?,?,?)",
                                 (rid, cid, motor, p["id"], f.get("url", ""), f.get("titulo", ""), d,
                                  int(f.get("url") in citadas or f.get("titulo") in citadas)))
            self.con.commit()
            return rid

    def borrar_respuesta(self, rid):
        with self.candado:
            for t in ("fanouts", "fuentes"):
                self.con.execute(f"DELETE FROM {t} WHERE respuesta_id=?", (rid,))
            self.con.execute("DELETE FROM respuestas WHERE id=?", (rid,))
            self.con.commit()
