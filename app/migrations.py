"""Migraciones explícitas del esquema de LidIA.

`init_db()` usa CREATE TABLE IF NOT EXISTS: sobre una base que ya existe no hace
nada. Todo cambio estructural sobre una base viva se hace acá, con número de
versión, de forma idempotente y verificable.

    002 — entidad EDICIÓN
    ---------------------
    courses          pasa a ser la MATERIA          (name UNIQUE, active)
    course_editions  es la EDICIÓN concreta         (UNIQUE(course_id, etiqueta))
    enrollments.course_id     -> edition_id
    course_teachers.course_id -> edition_id
    assignments.course_id     -> edition_id

    Cada curso actual se convierte en una materia (o se une a una ya derivada de
    otro curso) más su primera edición. La EDICIÓN CONSERVA EL id DEL CURSO: los
    valores de enrollments/course_teachers/assignments no se remapean, solo cambia
    el nombre de la columna. Eso hace que la verificación posterior sea una
    igualdad exacta de conjuntos, no una comparación aproximada, y que las URLs
    viejas (/admin/cursos/7) sigan apuntando a lo mismo.

    La materia conserva el id del curso más antiguo que la originó.

Uso (con el servicio detenido):

    python -m app.migrations --check                    # qué versión tiene la base
    python -m app.migrations --plan                     # informe: qué materia/etiqueta sale de cada curso
    python -m app.migrations --dry-run                  # aplica sobre una copia y verifica; no toca la base
    python -m app.migrations --apply                    # aplica de verdad (exige respaldo previo)

Deliberadamente NO se llama sola desde init_db(): una migración que corre al
arrancar la aplicación se ejecuta sin respaldo, sin nadie mirando y con uvicorn
compitiendo por el lock. Esta se corre a mano, una vez, con el servicio parado.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DB_PATH, utcnow  # noqa: E402

SCHEMA_VERSION = 2
CONFIG_KEY = "schema_version"


class MigrationAbort(RuntimeError):
    """Algo no cuadra. La transacción se revierte y la base queda como estaba."""


# --------------------------------------------------------------- heurística de nombre

# Solo cuenta un paréntesis FINAL, sin paréntesis anidados adentro. El `.*?` es
# perezoso y retrocede, de modo que en "IA (avanzada) (2026)" toma el último.
_PAREN_FINAL = re.compile(r"^(?P<base>.*?)\s*\((?P<label>[^()]*)\)$", re.DOTALL)


def split_course_name(name: str, default_label: str) -> tuple[str, str, str]:
    """Separa el nombre de un curso en (materia, etiqueta, motivo).

    Reglas, en orden:
      1. Se normalizan los espacios (colapsan, se recortan las puntas).
      2. Si el nombre termina en «(...)» sin paréntesis anidados, eso es la etiqueta
         y lo anterior es la materia.
      3. Paréntesis vacío «IA ()»: se descarta el paréntesis, etiqueta = default_label.
      4. Paréntesis sin materia adelante «(2026)»: NO se separa; sería una materia
         sin nombre. Queda entero como materia y etiqueta = default_label.
      5. Sin paréntesis final, paréntesis en el medio, o paréntesis sin cerrar:
         no se separa, etiqueta = default_label.

    `motivo` es para el informe previo; no altera el resultado.
    """
    n = " ".join((name or "").split())
    m = _PAREN_FINAL.match(n)
    if not m:
        return n, default_label, "sin-parentesis-final"
    base = m.group("base").strip()
    label = " ".join(m.group("label").split())
    if not base:
        return n, default_label, "parentesis-sin-materia"
    if not label:
        return base, default_label, "parentesis-vacio"
    return base, label, "separado"


def _clave_aproximada(s: str) -> str:
    """Clave laxa (minúsculas) solo para DETECTAR nombres casi iguales y avisar.

    No se unifica por acá: unificar «Aprendizaje Automático» con «aprendizaje
    automatico» cambia el nombre que ve el docente, y eso lo decide una persona.
    """
    return " ".join(s.lower().split())


def plan_editions(courses: list, anio: str) -> dict:
    """Arma el plan de migración a partir de las filas actuales de `courses`.

    `courses`: filas con id, name, active, created_at, ORDENADAS POR id ascendente.
    Devuelve {"materias": [...], "ediciones": [...], "avisos": [...]}.
    """
    materias: dict[str, dict] = {}      # nombre exacto de materia -> datos
    aprox: dict[str, str] = {}          # clave laxa -> primer nombre exacto visto
    ediciones: list[dict] = []
    avisos: list[str] = []

    for c in courses:
        cid, cname = int(c["id"]), c["name"]
        materia, etiqueta, motivo = split_course_name(cname, anio)

        ka = _clave_aproximada(materia)
        if ka in aprox and aprox[ka] != materia:
            avisos.append(
                f"Los cursos «{cname}» y los derivados de «{aprox[ka]}» producen materias que "
                f"difieren solo en mayúsculas o espacios («{materia}» vs «{aprox[ka]}»): "
                f"quedan como DOS materias distintas. Si tienen que ser una sola, unificá los "
                f"nombres en el panel ANTES de migrar."
            )
        aprox.setdefault(ka, materia)

        m = materias.get(materia)
        if m is None:
            m = materias[materia] = {
                "id": cid,                      # la materia hereda el id del curso más antiguo
                "name": materia,
                "active": 0,
                "created_at": c["created_at"],
                "etiquetas": {},                # etiqueta -> id de edición
            }
        else:
            avisos.append(
                f"«{cname}» se une a la materia «{materia}» (ya creada por otro curso): "
                f"una materia, dos ediciones."
            )
            if c["created_at"] < m["created_at"]:
                m["created_at"] = c["created_at"]

        # una materia está activa si alguna de sus ediciones lo está
        m["active"] = 1 if (m["active"] or int(c["active"])) else 0

        etiqueta_final = _etiqueta_libre(m["etiquetas"], etiqueta, cname, avisos, materia)
        m["etiquetas"][etiqueta_final] = cid

        if motivo == "separado" and not any(ch.isdigit() for ch in etiqueta_final):
            avisos.append(
                f"«{cname}» se separa en materia «{materia}» + edición «{etiqueta_final}», "
                f"y la etiqueta no parece un período lectivo. Revisalo antes de aplicar."
            )

        ediciones.append({
            "id": cid,                          # la edición conserva el id del curso
            "course_id": m["id"],
            "etiqueta": etiqueta_final,
            "active": int(c["active"]),
            "created_at": c["created_at"],
            "old_name": cname,
            "materia_name": materia,
            "motivo": motivo,
        })

    return {
        "materias": [
            {k: v for k, v in m.items() if k != "etiquetas"}
            for m in sorted(materias.values(), key=lambda x: x["id"])
        ],
        "ediciones": ediciones,
        "avisos": avisos,
    }


def _etiqueta_libre(usadas: dict, etiqueta: str, cname: str, avisos: list, materia: str) -> str:
    """Resuelve UNIQUE(course_id, etiqueta) cuando dos cursos dan la misma etiqueta.

    Cadena de reemplazo: etiqueta -> nombre original completo del curso -> etiqueta #2, #3…
    Nunca falla y nunca pisa una etiqueta ya asignada.
    """
    if etiqueta not in usadas:
        return etiqueta
    avisos.append(
        f"La etiqueta «{etiqueta}» de la materia «{materia}» ya estaba tomada al procesar "
        f"«{cname}»: se desambigua para no perder el curso. Renombrala después en el panel."
    )
    if cname not in usadas:
        return cname
    k = 2
    while f"{etiqueta} #{k}" in usadas:
        k += 1
    return f"{etiqueta} #{k}"


# --------------------------------------------------------------- DDL

DDL_MAP = """
CREATE TABLE _migracion_002_map (
    old_course_id  INTEGER PRIMARY KEY,   -- = course_editions.id
    old_name       TEXT NOT NULL,
    old_active     INTEGER NOT NULL,
    old_created_at TEXT NOT NULL,
    materia_id     INTEGER NOT NULL,
    materia_name   TEXT NOT NULL,
    etiqueta       TEXT NOT NULL,
    motivo         TEXT NOT NULL,
    migrado_at     TEXT NOT NULL
)
"""

DDL_EDITIONS = """
CREATE TABLE course_editions (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    etiqueta TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE (course_id, etiqueta)
)
"""

DDL_COURSES_NUEVA = """
CREATE TABLE courses_nueva (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
)
"""

DDL_ENROLLMENTS_NUEVA = """
CREATE TABLE enrollments_nueva (
    id INTEGER PRIMARY KEY,
    edition_id INTEGER NOT NULL REFERENCES course_editions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    UNIQUE (edition_id, user_id)
)
"""

DDL_TEACHERS_NUEVA = """
CREATE TABLE course_teachers_nueva (
    edition_id INTEGER NOT NULL REFERENCES course_editions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (edition_id, user_id)
)
"""

# assignments se reconstruye a partir de esta definición canónica, pero copiando
# solo las columnas que la base REALMENTE tenga (la de producción trae `tipo` y
# `respuestas` agregadas por ALTER, en otro orden que el de SCHEMA). Las que
# falten se crean con su DEFAULT.
ASSIGNMENTS_COLS = {
    "id":            "id INTEGER PRIMARY KEY",
    "edition_id":    "edition_id INTEGER NOT NULL REFERENCES course_editions(id) ON DELETE CASCADE",
    "name":          "name TEXT NOT NULL",
    "active":        "active INTEGER NOT NULL DEFAULT 0",
    "tipo":          "tipo TEXT NOT NULL DEFAULT 'abierto' CHECK (tipo IN ('abierto', 'escrito', 'choice'))",
    "consigna":      "consigna TEXT NOT NULL DEFAULT ''",
    "rubrica":       "rubrica TEXT NOT NULL DEFAULT ''",
    "respuestas":    "respuestas TEXT NOT NULL DEFAULT ''",
    "prompt_extra":  "prompt_extra TEXT NOT NULL DEFAULT ''",
    "max_practicas": "max_practicas INTEGER NOT NULL DEFAULT 3",
    "max_preguntas": "max_preguntas INTEGER NOT NULL DEFAULT 3",
    "created_at":    "created_at TEXT NOT NULL",
}
# columnas sin DEFAULT: si la base no las tiene, no se puede inventar el valor
ASSIGNMENTS_OBLIGATORIAS = {"id", "name", "created_at"}

# Índices explícitos. El esquema v1 no tenía ninguno (solo los automáticos de
# UNIQUE/PK). Acá se agrega el único que el modelo nuevo realmente necesita: la
# clave foránea course_editions.course_id, que no queda cubierta por ningún
# UNIQUE y se consulta en cada listado de ediciones de una materia.
DDL_INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_course_editions_course ON course_editions(course_id)",
]


# --------------------------------------------------------------- utilidades

def _cols(db, tabla: str) -> list[str]:
    return [r[1] for r in db.execute(f"PRAGMA table_info({tabla})").fetchall()]


def detect_version(db) -> int:
    """Versión del esquema, deducida de la estructura (no solo de config)."""
    tiene_ediciones = bool(_cols(db, "course_editions"))
    enr = _cols(db, "enrollments")
    if not enr:
        return 0  # base vacía: no hay nada que migrar, la crea init_db()
    tiene_edition_id = "edition_id" in enr
    if tiene_ediciones and tiene_edition_id:
        return 2
    if not tiene_ediciones and not tiene_edition_id:
        return 1
    raise MigrationAbort(
        "La base está a medio migrar: course_editions existe pero enrollments no tiene "
        "edition_id (o al revés). Esto no debería poder pasar porque la migración es "
        "transaccional. NO la sigas usando: restaurá el respaldo previo."
    )


def _snapshot(db, col: str) -> dict:
    """Conteos y huellas exactas de lo que no se puede perder.

    `col` es 'course_id' antes de migrar y 'edition_id' después: como la edición
    conserva el id del curso, los conjuntos tienen que dar IDÉNTICOS.
    """
    q = lambda s: db.execute(s).fetchone()[0]  # noqa: E731
    snap = {
        "n_cursos_o_ediciones": q("SELECT COUNT(*) FROM " + ("courses" if col == "course_id" else "course_editions")),
        "n_assignments": q("SELECT COUNT(*) FROM assignments"),
        "n_enrollments": q("SELECT COUNT(*) FROM enrollments"),
        "n_course_teachers": q("SELECT COUNT(*) FROM course_teachers"),
        "n_submissions": q("SELECT COUNT(*) FROM submissions"),
        "n_questions": q("SELECT COUNT(*) FROM questions"),
        "n_assignment_items": q("SELECT COUNT(*) FROM assignment_items"),
        "n_users": q("SELECT COUNT(*) FROM users"),
        "set_enrollments": {
            (r[0], r[1], r[2]) for r in
            db.execute(f"SELECT id, {col}, user_id FROM enrollments")
        },
        "set_teachers": {
            (r[0], r[1]) for r in db.execute(f"SELECT {col}, user_id FROM course_teachers")
        },
        "set_assignments": {
            (r[0], r[1], r[2]) for r in
            db.execute(f"SELECT id, {col}, name FROM assignments")
        },
        "hash_submissions": _hash_submissions(db),
        "hash_assignments_contenido": _hash_assignments(db),
    }
    return snap


def _hash_submissions(db) -> str:
    """Huella del contenido de las entregas y sus devoluciones, texto incluido."""
    h = hashlib.sha256()
    for r in db.execute(
        "SELECT id, user_id, assignment_id, kind, status, work_text, ai_feedback_md, "
        "final_feedback_md, reviewed_by, reviewed_at, created_at FROM submissions ORDER BY id"
    ):
        for v in r:
            h.update(repr(v).encode("utf-8"))
            h.update(b"\x1f")
    return h.hexdigest()


def _hash_assignments(db) -> str:
    """Huella del material de corrección: consigna, rúbrica, respuestas, cupos."""
    presentes = [c for c in ("id", "name", "active", "tipo", "consigna", "rubrica", "respuestas",
                             "prompt_extra", "max_practicas", "max_preguntas", "created_at")
                 if c in _cols(db, "assignments")]
    h = hashlib.sha256()
    for r in db.execute(f"SELECT {', '.join(presentes)} FROM assignments ORDER BY id"):
        for v in r:
            h.update(repr(v).encode("utf-8"))
            h.update(b"\x1f")
    return h.hexdigest()


def _comparar(antes: dict, despues: dict) -> list[str]:
    """Devuelve la lista de diferencias. Vacía = todo cuadra."""
    problemas = []
    for k in ("n_cursos_o_ediciones", "n_assignments", "n_enrollments", "n_course_teachers",
              "n_submissions", "n_questions", "n_assignment_items", "n_users"):
        if antes[k] != despues[k]:
            problemas.append(f"{k}: antes {antes[k]}, después {despues[k]}")
    for k in ("set_enrollments", "set_teachers", "set_assignments"):
        faltan = antes[k] - despues[k]
        sobran = despues[k] - antes[k]
        if faltan:
            problemas.append(f"{k}: faltan {len(faltan)} filas, p.ej. {sorted(faltan)[:3]}")
        if sobran:
            problemas.append(f"{k}: aparecieron {len(sobran)} filas de la nada, p.ej. {sorted(sobran)[:3]}")
    for k in ("hash_submissions", "hash_assignments_contenido"):
        if antes[k] != despues[k]:
            problemas.append(f"{k}: la huella cambió ({antes[k][:12]} -> {despues[k][:12]})")
    return problemas


# --------------------------------------------------------------- migración

def migrate_002(db_path: str = DB_PATH, anio: str | None = None, verbose: bool = True) -> str:
    """Aplica la migración 002. Idempotente: si ya está aplicada, no hace nada.

    Devuelve un texto con el resultado. Si algo no cuadra, revierte todo y levanta
    MigrationAbort: la base queda exactamente como estaba.
    """
    anio = anio or str(datetime.now().year)
    log = []
    say = (lambda s: (log.append(s), print(s))) if verbose else (lambda s: log.append(s))

    db = sqlite3.connect(db_path)
    db.isolation_level = None          # sin transacciones implícitas de Python: las manejo yo
    db.row_factory = sqlite3.Row
    try:
        v = detect_version(db)
        if v == 0:
            return "Base vacía (no hay tablas): la crea init_db(), no hay nada que migrar."
        if v >= SCHEMA_VERSION:
            _sellar_version(db)
            return f"Ya estaba en la versión {v}: no se hizo nada."

        # --- PRAGMAs: fuera de transacción, o son silenciosamente ignorados ---
        db.execute("PRAGMA foreign_keys = OFF")
        # Sin esto, `ALTER TABLE x RENAME TO y` reescribe las referencias de OTRAS
        # tablas (SQLite >= 3.25) y submissions terminaría apuntando a un fantasma.
        db.execute("PRAGMA legacy_alter_table = ON")
        if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise MigrationAbort(
                "No se pudo apagar foreign_keys (¿había una transacción abierta?). "
                "Abortado: con las claves foráneas encendidas, DROP TABLE courses dispara "
                "ON DELETE CASCADE y borra inscripciones, docentes e instancias."
            )

        db.execute("BEGIN IMMEDIATE")   # toma el lock de escritura ya; si hay otro proceso, falla acá
        try:
            _verificar_esquema_v1(db)
            antes = _snapshot(db, "course_id")
            say(f"Antes: {antes['n_cursos_o_ediciones']} cursos, {antes['n_assignments']} instancias, "
                f"{antes['n_enrollments']} inscripciones, {antes['n_course_teachers']} asignaciones "
                f"docentes, {antes['n_submissions']} entregas.")

            cursos = db.execute("SELECT id, name, active, created_at FROM courses ORDER BY id").fetchall()
            plan = plan_editions(cursos, anio)
            for a in plan["avisos"]:
                say("  aviso: " + a)

            _aplicar_plan(db, plan)
            _reconstruir_dependientes(db)
            _reemplazar_courses(db)
            for ddl in DDL_INDICES:
                db.execute(ddl)

            # --- verificación DENTRO de la transacción ---
            orfanas = db.execute("PRAGMA foreign_key_check").fetchall()
            if orfanas:
                raise MigrationAbort(f"foreign_key_check devolvió {len(orfanas)} filas huérfanas: "
                                     f"{[tuple(r) for r in orfanas[:5]]}")
            despues = _snapshot(db, "edition_id")
            problemas = _comparar(antes, despues)
            if plan["ediciones"] and len(plan["ediciones"]) != antes["n_cursos_o_ediciones"]:
                problemas.append("el plan no produjo una edición por cada curso")
            sin_materia = db.execute(
                "SELECT COUNT(*) FROM course_editions e "
                "LEFT JOIN courses c ON c.id = e.course_id WHERE c.id IS NULL"
            ).fetchone()[0]
            if sin_materia:
                problemas.append(f"{sin_materia} ediciones quedaron sin materia")
            if problemas:
                raise MigrationAbort("La verificación posterior falló:\n  - " + "\n  - ".join(problemas))

            _sellar_version(db)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.execute("PRAGMA legacy_alter_table = OFF")
            db.execute("PRAGMA foreign_keys = ON")

        integridad = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integridad != "ok":
            raise MigrationAbort(f"integrity_check después de commitear: {integridad}")

        n_mat = db.execute("SELECT COUNT(*) FROM courses").fetchone()[0]
        n_ed = db.execute("SELECT COUNT(*) FROM course_editions").fetchone()[0]
        say(f"Después: {n_mat} materias, {n_ed} ediciones. Todo lo demás intacto. Versión {SCHEMA_VERSION}.")
        return "\n".join(log)
    finally:
        db.close()


def _verificar_esquema_v1(db):
    """Se niega a tocar una base cuya forma no es la esperada."""
    esperado = {
        "courses": {"id", "name", "active", "created_at"},
        "enrollments": {"id", "course_id", "user_id", "created_at"},
        "course_teachers": {"course_id", "user_id"},
    }
    for tabla, cols in esperado.items():
        reales = set(_cols(db, tabla))
        if reales != cols:
            raise MigrationAbort(
                f"La tabla {tabla} no tiene las columnas esperadas.\n"
                f"  esperadas: {sorted(cols)}\n  reales:    {sorted(reales)}\n"
                f"La migración está escrita para el esquema v1; revisá qué pasó antes de seguir."
            )
    reales = set(_cols(db, "assignments"))
    desconocidas = reales - set(ASSIGNMENTS_COLS) - {"course_id"}
    if desconocidas:
        raise MigrationAbort(
            f"assignments tiene columnas que esta migración no conoce y por lo tanto no copiaría: "
            f"{sorted(desconocidas)}. Agregalas a ASSIGNMENTS_COLS antes de correrla."
        )
    faltantes = ASSIGNMENTS_OBLIGATORIAS - reales
    if faltantes or "course_id" not in reales:
        raise MigrationAbort(f"assignments no tiene columnas imprescindibles: "
                             f"{sorted(faltantes | ({'course_id'} - reales))}")


def _aplicar_plan(db, plan: dict):
    ahora = utcnow()
    db.execute(DDL_MAP)
    db.executemany(
        "INSERT INTO _migracion_002_map (old_course_id, old_name, old_active, old_created_at, "
        "materia_id, materia_name, etiqueta, motivo, migrado_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(e["id"], e["old_name"], e["active"], e["created_at"], e["course_id"],
          e["materia_name"], e["etiqueta"], e["motivo"], ahora) for e in plan["ediciones"]],
    )
    db.execute(DDL_COURSES_NUEVA)
    db.executemany(
        "INSERT INTO courses_nueva (id, name, active, created_at) VALUES (?,?,?,?)",
        [(m["id"], m["name"], m["active"], m["created_at"]) for m in plan["materias"]],
    )
    db.execute(DDL_EDITIONS)
    db.executemany(
        "INSERT INTO course_editions (id, course_id, etiqueta, active, created_at) VALUES (?,?,?,?,?)",
        [(e["id"], e["course_id"], e["etiqueta"], e["active"], e["created_at"])
         for e in plan["ediciones"]],
    )


def _reconstruir_dependientes(db):
    """course_id -> edition_id en las tres tablas que cuelgan del curso.

    SQLite no sabe cambiar una clave foránea ni renombrar una columna manteniendo
    la FK: tabla nueva, copia con las columnas NOMBRADAS (nunca SELECT *, porque
    el orden de columnas de producción no es el de SCHEMA), DROP y RENAME.
    """
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
        raise MigrationAbort("foreign_keys quedó encendido: DROP TABLE dispararía los CASCADE.")

    db.execute(DDL_ENROLLMENTS_NUEVA)
    db.execute("INSERT INTO enrollments_nueva (id, edition_id, user_id, created_at) "
               "SELECT id, course_id, user_id, created_at FROM enrollments")
    db.execute("DROP TABLE enrollments")
    db.execute("ALTER TABLE enrollments_nueva RENAME TO enrollments")

    db.execute(DDL_TEACHERS_NUEVA)
    db.execute("INSERT INTO course_teachers_nueva (edition_id, user_id) "
               "SELECT course_id, user_id FROM course_teachers")
    db.execute("DROP TABLE course_teachers")
    db.execute("ALTER TABLE course_teachers_nueva RENAME TO course_teachers")

    presentes = [c for c in _cols(db, "assignments") if c != "course_id"]
    destino = ["edition_id"] + presentes
    origen = ["course_id"] + presentes
    faltantes = [c for c in ASSIGNMENTS_COLS if c not in destino]   # se crean con su DEFAULT
    ddl = ",\n    ".join(ASSIGNMENTS_COLS[c] for c in ASSIGNMENTS_COLS)
    db.execute(f"CREATE TABLE assignments_nueva (\n    {ddl},\n    UNIQUE (edition_id, name)\n)")
    db.execute(
        f"INSERT INTO assignments_nueva ({', '.join(destino)}) "
        f"SELECT {', '.join(origen)} FROM assignments"
    )
    db.execute("DROP TABLE assignments")
    db.execute("ALTER TABLE assignments_nueva RENAME TO assignments")
    if faltantes:
        print(f"  nota: assignments no tenía {faltantes}; se crearon con su valor por defecto.")


def _reemplazar_courses(db):
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
        raise MigrationAbort("foreign_keys quedó encendido antes de DROP TABLE courses.")
    db.execute("DROP TABLE courses")
    db.execute("ALTER TABLE courses_nueva RENAME TO courses")


def _sellar_version(db):
    db.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (CONFIG_KEY, str(SCHEMA_VERSION)),
    )
    db.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (f"{CONFIG_KEY}_{SCHEMA_VERSION}_at", utcnow()),
    )
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


# --------------------------------------------------------------- línea de comandos

def _informe_plan(db_path: str, anio: str) -> str:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        if detect_version(db) >= SCHEMA_VERSION:
            return "La base ya está migrada."
        cursos = db.execute("SELECT id, name, active, created_at FROM courses ORDER BY id").fetchall()
        plan = plan_editions(cursos, anio)
        out = [f"{len(cursos)} cursos -> {len(plan['materias'])} materias + {len(plan['ediciones'])} ediciones", ""]
        ancho = max([len(c["name"]) for c in cursos] + [6])
        for e in plan["ediciones"]:
            out.append(f"  [{e['id']:>3}] {e['old_name']:<{ancho}}  ->  materia [{e['course_id']:>3}] "
                       f"«{e['materia_name']}»  edición «{e['etiqueta']}»   ({e['motivo']})")
        if plan["avisos"]:
            out += ["", "Avisos:"] + [f"  - {a}" for a in plan["avisos"]]
        return "\n".join(out)
    finally:
        db.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Migraciones de la base de LidIA")
    p.add_argument("--db", default=DB_PATH)
    p.add_argument("--anio", default=str(datetime.now().year),
                   help="etiqueta para los cursos sin paréntesis (por defecto, el año en curso)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="informa la versión del esquema")
    g.add_argument("--plan", action="store_true", help="qué materia y etiqueta sale de cada curso")
    g.add_argument("--dry-run", action="store_true", help="migra una COPIA y verifica; no toca la base")
    g.add_argument("--apply", action="store_true", help="migra la base de verdad")
    a = p.parse_args(argv)

    if not os.path.exists(a.db):
        print(f"No existe {a.db}", file=sys.stderr)
        return 2

    if a.check:
        db = sqlite3.connect(a.db)
        try:
            print(f"{a.db}: esquema v{detect_version(db)} "
                  f"(config.{CONFIG_KEY} = "
                  f"{dict(db.execute('SELECT key, value FROM config')).get(CONFIG_KEY, '—')})")
        finally:
            db.close()
        return 0

    if a.plan:
        print(_informe_plan(a.db, a.anio))
        return 0

    if a.dry_run:
        copia = a.db + ".ensayo"
        _copia_consistente(a.db, copia)
        print(f"Ensayo sobre {copia} (la base real no se toca)\n")
        try:
            migrate_002(copia, a.anio)
            print("\nEl ensayo pasó todas las verificaciones.")
            return 0
        except MigrationAbort as e:
            print(f"\nEL ENSAYO FALLÓ: {e}", file=sys.stderr)
            return 1
        finally:
            os.remove(copia)

    # --apply
    resguardo = f"{a.db}.v1-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    _copia_consistente(a.db, resguardo)
    print(f"Respaldo: {resguardo}")
    try:
        migrate_002(a.db, a.anio)
        return 0
    except MigrationAbort as e:
        print(f"\nMIGRACIÓN ABORTADA (la base quedó como estaba): {e}", file=sys.stderr)
        print(f"Respaldo disponible en {resguardo}", file=sys.stderr)
        return 1


def _copia_consistente(origen: str, destino: str):
    """Copia en caliente por la API de respaldo de SQLite (no `cp`: `cp` puede
    copiar una base a mitad de una escritura)."""
    src = sqlite3.connect(f"file:{origen}?mode=ro", uri=True)
    dst = sqlite3.connect(destino)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    shutil.copystat(origen, destino)


if __name__ == "__main__":
    raise SystemExit(main())
