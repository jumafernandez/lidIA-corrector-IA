"""Pruebas de la migración 002 (entidad EDICIÓN).

    python3 tests/test_migracion_002.py        # sin dependencias, no necesita pytest

Cubre la heurística de separación del nombre, el plan de unificación, la
migración completa sobre una base sintética con los casos borde, la idempotencia
y la vuelta atrás por SQL (ida y vuelta = la base original).
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.migrations import (MigrationAbort, detect_version, migrate_002,  # noqa: E402
                            plan_editions, split_course_name)

FALLOS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        print(f"  FALLA {msg}")
        FALLOS.append(msg)


def eq(a, b, msg):
    check(a == b, f"{msg}   (obtenido {a!r}, esperado {b!r})" if a != b else msg)


# --------------------------------------------------------------- 1. heurística

def test_split():
    print("\n[1] separación nombre -> materia + etiqueta")
    casos = [
        # (nombre, esperado_materia, esperada_etiqueta)
        ("Introducción a la Inteligencia Artificial (2026)", "Introducción a la Inteligencia Artificial", "2026"),
        ("Aprendizaje Automático (2026 2C)", "Aprendizaje Automático", "2026 2C"),
        ("Bases de Datos (Contracursada 2025)", "Bases de Datos", "Contracursada 2025"),
        # sin paréntesis -> año en curso
        ("Aprendizaje Automático", "Aprendizaje Automático", "AÑO"),
        # paréntesis en el MEDIO: no es una etiqueta
        ("Álgebra (I) para Informática", "Álgebra (I) para Informática", "AÑO"),
        # paréntesis vacío: se descarta el paréntesis
        ("Redes Neuronales ()", "Redes Neuronales", "AÑO"),
        ("Redes Neuronales (   )", "Redes Neuronales", "AÑO"),
        # sin cerrar / sin abrir: no se separa
        ("Minería de Datos (2026", "Minería de Datos (2026", "AÑO"),
        ("Minería de Datos 2026)", "Minería de Datos 2026)", "AÑO"),
        # paréntesis sin materia adelante: dejaría una materia sin nombre
        ("(2026)", "(2026)", "AÑO"),
        # dos paréntesis: manda el último
        ("Taller (avanzado) (2026)", "Taller (avanzado)", "2026"),
        # espacios de más, en cualquier lado
        ("  Ciencia   de  Datos   ( 2026 2C )  ", "Ciencia de Datos", "2026 2C"),
        ("Ética y IA(2026)", "Ética y IA", "2026"),
        # anidados: no hay forma sensata de partirlo, se deja entero
        ("Seminario (IA (aplicada))", "Seminario (IA (aplicada))", "AÑO"),
        # nombre vacío
        ("", "", "AÑO"),
    ]
    for nombre, materia, etiqueta in casos:
        m, e, _ = split_course_name(nombre, "AÑO")
        eq((m, e), (materia, etiqueta), f"«{nombre}»")


# --------------------------------------------------------------- 2. plan

def _cursos(*nombres):
    return [{"id": i, "name": n, "active": 1, "created_at": f"2026-01-{i:02d} 00:00:00"}
            for i, n in enumerate(nombres, 1)]


def test_plan():
    print("\n[2] plan: unificación, colisiones, ids")

    # dos cursos que dan la misma materia -> UNA materia, DOS ediciones
    p = plan_editions(_cursos("Aprendizaje Automático (2025)", "Aprendizaje Automático (2026)"), "2026")
    eq(len(p["materias"]), 1, "dos ediciones de la misma materia se unifican")
    eq(len(p["ediciones"]), 2, "no se pierde ningún curso")
    eq(p["materias"][0]["id"], 1, "la materia hereda el id del curso más antiguo")
    eq([e["id"] for e in p["ediciones"]], [1, 2], "la edición conserva el id del curso")
    eq([e["etiqueta"] for e in p["ediciones"]], ["2025", "2026"], "etiquetas")

    # el que ya venía sin paréntesis se une al mismo tronco
    p = plan_editions(_cursos("Aprendizaje Automático", "Aprendizaje Automático (2026 2C)"), "2026")
    eq(len(p["materias"]), 1, "«X» y «X (2026 2C)» son la misma materia")
    eq([e["etiqueta"] for e in p["ediciones"]], ["2026", "2026 2C"], "al pelado le toca el año en curso")

    # colisión de etiqueta dentro de la misma materia: nadie se pierde
    p = plan_editions(_cursos("Bases de Datos", "Bases de Datos (2026)"), "2026")
    eq(len(p["ediciones"]), 2, "colisión de etiqueta: siguen siendo dos ediciones")
    eq(len(set(e["etiqueta"] for e in p["ediciones"])), 2, "las etiquetas quedan distintas")
    check(any("ya estaba tomada" in a for a in p["avisos"]), "la colisión queda avisada")

    # triple colisión
    p = plan_editions(_cursos("Redes", "Redes (2026)", "Redes ()"), "2026")
    eq(len(p["materias"]), 1, "tres cursos, una materia")
    eq(len(set(e["etiqueta"] for e in p["ediciones"])), 3, "tres etiquetas distintas")

    # variantes de mayúsculas: NO se unifican solas, pero se avisa
    p = plan_editions(_cursos("Aprendizaje Automático (2026)", "aprendizaje automático (2025)"), "2026")
    eq(len(p["materias"]), 2, "las variantes de mayúsculas quedan separadas")
    check(any("difieren solo en mayúsculas" in a for a in p["avisos"]), "la casi-colisión queda avisada")

    # etiqueta que no parece un período lectivo
    p = plan_editions(_cursos("Programación (avanzada)"), "2026")
    check(any("no parece un período lectivo" in a for a in p["avisos"]), "etiqueta sospechosa avisada")

    # la materia está activa si alguna edición lo está
    cs = _cursos("Física (2025)", "Física (2026)")
    cs[0]["active"] = 0
    p = plan_editions(cs, "2026")
    eq(p["materias"][0]["active"], 1, "materia activa si alguna edición lo está")
    eq([e["active"] for e in p["ediciones"]], [0, 1], "cada edición conserva su propio activo")


# --------------------------------------------------------------- 3. base sintética

V1 = """
CREATE TABLE users (id INTEGER PRIMARY KEY, login TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    initial_password TEXT, full_name TEXT NOT NULL, email TEXT DEFAULT '', role TEXT NOT NULL
    CHECK (role IN ('admin','docente','student')), active INTEGER NOT NULL DEFAULT 1,
    profile TEXT DEFAULT '', created_at TEXT NOT NULL, avatar BLOB, avatar_mime TEXT DEFAULT '');
CREATE TABLE courses (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
-- ojo: tipo y respuestas al final, como quedaron en producción por ALTER TABLE
CREATE TABLE assignments (id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE, name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0, consigna TEXT NOT NULL DEFAULT '', rubrica TEXT NOT NULL DEFAULT '',
    prompt_extra TEXT NOT NULL DEFAULT '', max_practicas INTEGER NOT NULL DEFAULT 3,
    max_preguntas INTEGER NOT NULL DEFAULT 3, created_at TEXT NOT NULL,
    tipo TEXT NOT NULL DEFAULT 'abierto', respuestas TEXT NOT NULL DEFAULT '',
    UNIQUE (course_id, name));
CREATE TABLE assignment_items (id INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    orden INTEGER NOT NULL DEFAULT 0, enunciado TEXT NOT NULL DEFAULT '', respuesta TEXT NOT NULL DEFAULT '',
    opciones TEXT NOT NULL DEFAULT '', puntaje REAL NOT NULL DEFAULT 1);
CREATE TABLE course_teachers (course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, PRIMARY KEY (course_id, user_id));
CREATE TABLE enrollments (id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL,
    UNIQUE (course_id, user_id));
CREATE TABLE sessions (token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL);
CREATE TABLE submissions (id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    kind TEXT NOT NULL CHECK (kind IN ('practica','final')), status TEXT NOT NULL,
    original_filename TEXT DEFAULT '', work_text TEXT NOT NULL DEFAULT '',
    text_chars INTEGER NOT NULL DEFAULT 0, truncated INTEGER NOT NULL DEFAULT 0,
    ai_feedback_md TEXT DEFAULT '', final_feedback_md TEXT DEFAULT '', model_used TEXT DEFAULT '',
    error TEXT DEFAULT '', reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT,
    created_at TEXT NOT NULL);
CREATE TABLE questions (id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    question TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

NOMBRES = [
    "Introducción a la Inteligencia Artificial (2026)",
    "Aprendizaje Automático (2025)",
    "Aprendizaje Automático (2026 2C)",
    "Aprendizaje Automático",                 # colisiona con la etiqueta del año
    "Álgebra (I) para Informática",           # paréntesis en el medio
    "Redes Neuronales ()",                    # paréntesis vacío
    "Minería de Datos (2026",                 # sin cerrar
    "Taller (avanzado) (2026)",               # dos paréntesis
]


def base_sintetica(path):
    db = sqlite3.connect(path)
    db.executescript(V1)
    db.execute("PRAGMA foreign_keys = ON")
    t = "2026-03-01 10:00:00"
    for i, n in enumerate(NOMBRES, 1):
        db.execute("INSERT INTO courses (id, name, active, created_at) VALUES (?,?,?,?)",
                   (i, n, 1 if i != 2 else 0, t))
    for i in range(1, 6):
        db.execute("INSERT INTO users (id, login, password_hash, full_name, role, created_at) "
                   "VALUES (?,?,?,?,?,?)", (i, f"u{i}", "x", f"Usuario {i}",
                                            "docente" if i < 3 else "student", t))
    aid = 0
    for cid in range(1, len(NOMBRES) + 1):
        db.execute("INSERT INTO course_teachers (course_id, user_id) VALUES (?, ?)", (cid, 1 + cid % 2))
        for u in (3, 4, 5):
            db.execute("INSERT INTO enrollments (course_id, user_id, created_at) VALUES (?,?,?)", (cid, u, t))
        for k in ("TP1", "Parcial"):
            aid += 1
            db.execute("INSERT INTO assignments (id, course_id, name, active, consigna, rubrica, "
                       "created_at, tipo, respuestas) VALUES (?,?,?,1,?,?,?,'abierto','clave')",
                       (aid, cid, k, f"consigna {cid}/{k}", f"rúbrica {cid}/{k}", t))
            db.execute("INSERT INTO assignment_items (assignment_id, orden, enunciado, respuesta) "
                       "VALUES (?,1,?,?)", (aid, "¿?", "porque sí"))
            for u in (3, 4):
                cur = db.execute(
                    "INSERT INTO submissions (user_id, assignment_id, kind, status, work_text, "
                    "ai_feedback_md, final_feedback_md, created_at) VALUES (?,?,'final','aprobada',?,?,?,?)",
                    (u, aid, f"trabajo de {u} en {aid}", f"devolución IA {u}/{aid}",
                     f"devolución final {u}/{aid}", t))
                db.execute("INSERT INTO questions (submission_id, question, answer, created_at) "
                           "VALUES (?,?,?,?)", (cur.lastrowid, "duda", "respuesta", t))
    db.commit()
    db.close()


def conteos(path):
    db = sqlite3.connect(path)
    try:
        return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("assignments", "enrollments", "course_teachers", "submissions",
                          "questions", "assignment_items", "users")}
    finally:
        db.close()


def test_migracion_completa():
    print("\n[3] migración sobre base sintética con todos los casos borde")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "lidia.db")
    base_sintetica(p)
    antes = conteos(p)

    migrate_002(p, anio="2026", verbose=False)
    despues = conteos(p)

    eq(despues, antes, "ningún conteo cambió")

    db = sqlite3.connect(p)
    db.row_factory = sqlite3.Row
    eq(detect_version(db), 2, "la base quedó en v2")
    eq(db.execute("SELECT value FROM config WHERE key='schema_version'").fetchone()[0], "2",
       "config.schema_version = 2")
    eq(db.execute("PRAGMA user_version").fetchone()[0], 2, "user_version = 2")
    eq(db.execute("SELECT COUNT(*) FROM course_editions").fetchone()[0], len(NOMBRES),
       "una edición por curso viejo")
    eq(db.execute("SELECT COUNT(*) FROM courses").fetchone()[0], len(NOMBRES) - 2,
       "las tres «Aprendizaje Automático» colapsan en una materia")
    check(db.execute("PRAGMA foreign_keys").fetchone()[0] == 1, "foreign_keys quedó encendido")
    eq(list(db.execute("PRAGMA foreign_key_check")), [], "sin filas huérfanas")
    eq(db.execute("PRAGMA integrity_check").fetchone()[0], "ok", "integrity_check")

    # los ids se conservan: es lo que hace verificable la migración
    eq(db.execute("SELECT COUNT(*) FROM enrollments WHERE edition_id NOT IN "
                  "(SELECT id FROM course_editions)").fetchone()[0], 0, "sin inscripciones huérfanas")
    eq(sorted(r[0] for r in db.execute("SELECT id FROM course_editions")),
       list(range(1, len(NOMBRES) + 1)), "las ediciones conservan los ids de los cursos")
    eq(db.execute("SELECT etiqueta FROM course_editions WHERE id = 5").fetchone()[0], "2026",
       "«Álgebra (I) para Informática» no se parte por el paréntesis del medio")
    eq(db.execute("SELECT c.name FROM course_editions e JOIN courses c ON c.id = e.course_id "
                  "WHERE e.id = 5").fetchone()[0], "Álgebra (I) para Informática",
       "…y conserva el nombre entero como materia")
    eq(db.execute("SELECT c.name FROM course_editions e JOIN courses c ON c.id = e.course_id "
                  "WHERE e.id = 8").fetchone()[0], "Taller (avanzado)", "manda el último paréntesis")

    # el submission sigue apuntando a su instancia y su instancia a su edición
    fila = db.execute(
        "SELECT s.final_feedback_md, a.name, e.etiqueta, c.name AS materia FROM submissions s "
        "JOIN assignments a ON a.id = s.assignment_id JOIN course_editions e ON e.id = a.edition_id "
        "JOIN courses c ON c.id = e.course_id WHERE s.id = 1").fetchone()
    eq((fila["name"], fila["materia"], fila["etiqueta"]),
       ("TP1", "Introducción a la Inteligencia Artificial", "2026"),
       "una entrega sigue llegando hasta su materia y su edición")

    # el equipo docente pasó a colgar de la edición
    eq(db.execute("SELECT COUNT(DISTINCT edition_id) FROM course_teachers").fetchone()[0], len(NOMBRES),
       "cada edición conserva su equipo docente")

    # la tabla de correspondencia queda para auditar y para volver atrás
    eq(db.execute("SELECT old_name FROM _migracion_002_map WHERE old_course_id = 4").fetchone()[0],
       "Aprendizaje Automático", "el nombre original queda registrado")
    db.close()

    # idempotencia
    salida = migrate_002(p, anio="2026", verbose=False)
    check("Ya estaba en la versión 2" in salida, "correrla de nuevo no hace nada")
    eq(conteos(p), antes, "y no cambia ningún conteo")
    return p


def test_rechaza_esquema_raro():
    print("\n[4] se niega a migrar una base que no entiende")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "raro.db")
    base_sintetica(p)
    db = sqlite3.connect(p)
    db.execute("ALTER TABLE assignments ADD COLUMN inventada TEXT")
    db.commit()
    db.close()
    try:
        migrate_002(p, anio="2026", verbose=False)
        check(False, "tendría que haber abortado")
    except MigrationAbort as e:
        check("inventada" in str(e), "aborta nombrando la columna desconocida")
    db = sqlite3.connect(p)
    eq(detect_version(db), 1, "la base quedó intacta en v1")
    db.close()


def test_reversion():
    print("\n[5] vuelta atrás por SQL: ida y vuelta devuelve la base original")
    d = tempfile.mkdtemp()
    orig = os.path.join(d, "orig.db")
    base_sintetica(orig)
    ida = os.path.join(d, "ida.db")
    base_sintetica(ida)
    migrate_002(ida, anio="2026", verbose=False)

    sql = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "deploy", "reversion_002_ediciones.sql")).read()
    db = sqlite3.connect(ida)
    db.isolation_level = None
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("PRAGMA legacy_alter_table = ON")
    db.executescript(sql)
    db.execute("PRAGMA foreign_keys = ON")
    eq(list(db.execute("PRAGMA foreign_key_check")), [], "sin huérfanas después de revertir")
    eq(detect_version(db), 1, "volvió a v1")

    def volcado(path):
        c = sqlite3.connect(path)
        try:
            return [l for l in c.iterdump()
                    if "_migracion_002_map" not in l and "schema_version" not in l]
        finally:
            c.close()
    db.close()
    a, b = volcado(orig), volcado(ida)
    eq(len(a), len(b), "mismo número de sentencias en el volcado")
    dif = [(x, y) for x, y in zip(a, b) if x != y]
    check(not dif, f"el volcado coincide con el original ({dif[:2]})")


if __name__ == "__main__":
    test_split()
    test_plan()
    p = test_migracion_completa()
    test_rechaza_esquema_raro()
    test_reversion(p)
    print("\n" + ("TODO BIEN" if not FALLOS else f"{len(FALLOS)} FALLAS:\n  " + "\n  ".join(FALLOS)))
    raise SystemExit(1 if FALLOS else 0)
