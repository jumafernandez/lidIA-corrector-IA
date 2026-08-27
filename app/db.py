"""Base de datos SQLite: esquema, conexión y configuración."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "lidia.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    login TEXT NOT NULL UNIQUE,            -- DNI para estudiantes, usuario para docentes/coordinación
    password_hash TEXT NOT NULL,
    initial_password TEXT,                 -- solo para repartir credenciales (quitar en producción)
    full_name TEXT NOT NULL,
    email TEXT DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('admin', 'docente', 'student')),
    active INTEGER NOT NULL DEFAULT 1,     -- deshabilitado = no genera devoluciones ni entrega final
    profile TEXT DEFAULT '',               -- perfil de corrección (orientación de la devolución)
    avatar BLOB,                           -- foto de perfil (opcional, ≤1 MB)
    avatar_mime TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

-- La MATERIA: el nombre estable, sin año ni cuatrimestre.
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,     -- inactiva = no se ofrece para ediciones nuevas
    created_at TEXT NOT NULL
);

-- La EDICIÓN: una cursada concreta de esa materia («2026», «2026 2C», «Contracursada 2025»).
-- Todo lo que ocurre durante una cursada —estudiantes, docentes, instancias— cuelga de acá.
CREATE TABLE IF NOT EXISTS course_editions (
    id INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    etiqueta TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,     -- inactiva = cursada cerrada (solo lectura)
    created_at TEXT NOT NULL,
    UNIQUE (course_id, etiqueta)
);
CREATE INDEX IF NOT EXISTS idx_course_editions_course ON course_editions(course_id);

-- Instancias de evaluación de una edición (TP, parcial domiciliario, trabajo final...).
-- tipo: 'abierto' (consigna + rúbrica), 'escrito' (examen con respuestas esperadas),
--       'choice' (multiple choice con clave; entrega única, sin prácticas).
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    edition_id INTEGER NOT NULL REFERENCES course_editions(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,     -- se activa cuando el material de corrección está listo
    tipo TEXT NOT NULL DEFAULT 'abierto' CHECK (tipo IN ('abierto', 'escrito', 'choice')),
    consigna TEXT NOT NULL DEFAULT '',
    rubrica TEXT NOT NULL DEFAULT '',
    respuestas TEXT NOT NULL DEFAULT '',   -- estándar de corrección (respuestas esperadas / clave); solo docentes
    prompt_extra TEXT NOT NULL DEFAULT '',
    max_practicas INTEGER NOT NULL DEFAULT 3,
    max_preguntas INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    UNIQUE (edition_id, name)
);

-- Preguntas de una instancia (por ahora, exámenes escritos). El campo opciones
-- queda reservado para cuando el multiple choice también pase a ítems.
CREATE TABLE IF NOT EXISTS assignment_items (
    id INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    orden INTEGER NOT NULL DEFAULT 0,
    enunciado TEXT NOT NULL DEFAULT '',
    respuesta TEXT NOT NULL DEFAULT '',    -- respuesta esperada; material interno
    opciones TEXT NOT NULL DEFAULT '',     -- reservado para multiple choice
    puntaje REAL NOT NULL DEFAULT 1
);

-- El equipo docente es de la edición: cambia de una cursada a la otra.
CREATE TABLE IF NOT EXISTS course_teachers (
    edition_id INTEGER NOT NULL REFERENCES course_editions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (edition_id, user_id)
);

-- Quien recursa queda inscripto en dos ediciones de la misma materia, con sus
-- cupos y sus entregas separados.
CREATE TABLE IF NOT EXISTS enrollments (
    id INTEGER PRIMARY KEY,
    edition_id INTEGER NOT NULL REFERENCES course_editions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    UNIQUE (edition_id, user_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    kind TEXT NOT NULL CHECK (kind IN ('practica', 'final')),
    status TEXT NOT NULL,                  -- practica: 'ok' | 'error' ; final: 'pendiente' | 'aprobada' | 'reabierta'
    original_filename TEXT DEFAULT '',
    work_text TEXT NOT NULL DEFAULT '',
    text_chars INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    ai_feedback_md TEXT DEFAULT '',        -- devolución generada por la IA
    final_feedback_md TEXT DEFAULT '',     -- devolución final (editada/aprobada por docente)
    model_used TEXT DEFAULT '',
    error TEXT DEFAULT '',
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Configuración global (transversal). La consigna, rúbrica, indicaciones y cupos
# viven en cada instancia de evaluación.
DEFAULT_CONFIG = {
    "enviar_nombre": "1",  # enviar nombre de pila del estudiante al modelo
    "banner_deshabilitado": (
        "Tu espacio sigue activo y podés consultar tu historial. Para habilitar nuevas devoluciones y la entrega final, "
        "acercate al staff de tu carrera para regularizar lo administrativo."
    ),
}

# Ejemplo con el que se siembra la instancia inicial (seed.py); las instancias
# nuevas arrancan vacías y las completa el docente.
DEMO_CONSIGNA = (
    "Trabajo Final Integrador de la Diplomatura de Posgrado en Inteligencia Artificial Generativa. "
    "Desarrollá una solución de IA generativa aplicada a un problema real, integrando los cuatro módulos: "
    "fundamentos de IA, infraestructura cloud, deep learning e IA generativa. La entrega es un informe que "
    "describa el problema, la arquitectura de la solución, la implementación (modelo, despliegue, infraestructura) "
    "y la evaluación de resultados."
)
DEMO_RUBRICA = (
    "1. Problema y justificación: relevancia del problema elegido, claridad de objetivos y alcance.\n"
    "2. Arquitectura de la solución: diseño de la solución de IA generativa (modelo, prompts, RAG/fine-tuning si aplica), "
    "decisiones fundamentadas.\n"
    "3. Implementación e infraestructura: uso de infraestructura cloud, reproducibilidad, buenas prácticas.\n"
    "4. Evaluación de resultados: métricas o criterios de evaluación, análisis crítico de limitaciones.\n"
    "5. Calidad del informe: organización, claridad, uso correcto de terminología, citas y referencias."
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def get_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.executescript(SCHEMA)
        # migraciones sobre bases existentes (CREATE IF NOT EXISTS no altera tablas viejas)
        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
        if "avatar" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN avatar BLOB")
            db.execute("ALTER TABLE users ADD COLUMN avatar_mime TEXT DEFAULT ''")
        for key, value in DEFAULT_CONFIG.items():
            db.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (key, value))


def get_config(db) -> dict:
    rows = db.execute("SELECT key, value FROM config").fetchall()
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({r["key"]: r["value"] for r in rows})
    return cfg


def set_config(db, key: str, value: str):
    db.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------------------------------------------------------------- materias

def get_course(db, course_id: int):
    """La materia (el nombre estable, sin período)."""
    return db.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()


def all_courses(db, only_active: bool = False):
    q = "SELECT * FROM courses"
    if only_active:
        q += " WHERE active = 1"
    return db.execute(q + " ORDER BY name").fetchall()


# ---------------------------------------------------------------- ediciones
#
# `nombre` es el nombre completo que se muestra en pantalla («Álgebra 2026 2C»);
# se arma en SQL para que las plantillas no tengan que componerlo en cada lugar.

EDICION_SELECT = """
SELECT ed.*, c.name AS materia, c.active AS materia_active,
       c.name || ' ' || ed.etiqueta AS nombre,
       c.name || ' ' || ed.etiqueta AS name
  FROM course_editions ed JOIN courses c ON c.id = ed.course_id
"""


def get_edition(db, edition_id: int):
    return db.execute(EDICION_SELECT + " WHERE ed.id = ?", (edition_id,)).fetchone()


def course_editions(db, course_id: int):
    """Ediciones de una materia, de la más reciente a la más vieja."""
    return db.execute(
        EDICION_SELECT + " WHERE ed.course_id = ? ORDER BY ed.created_at DESC, ed.id DESC",
        (course_id,),
    ).fetchall()


def teacher_edition_ids(db, user) -> list | None:
    """Ediciones que este usuario staff administra. None = todas (coordinación)."""
    if user["role"] == "admin":
        return None
    rows = db.execute("SELECT edition_id FROM course_teachers WHERE user_id = ?", (user["id"],)).fetchall()
    return [r["edition_id"] for r in rows]


def staff_editions(db, user):
    """Ediciones visibles para un usuario staff, agrupables por materia."""
    ids = teacher_edition_ids(db, user)
    orden = " ORDER BY c.name, ed.created_at DESC, ed.etiqueta"
    if ids is None:
        return db.execute(EDICION_SELECT + orden).fetchall()
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return db.execute(EDICION_SELECT + f" WHERE ed.id IN ({marks})" + orden, ids).fetchall()


def can_access_edition(db, user, edition_id: int) -> bool:
    ids = teacher_edition_ids(db, user)
    return ids is None or edition_id in ids


def edition_teachers(db, edition_id: int):
    return db.execute(
        "SELECT u.* FROM users u JOIN course_teachers ct ON ct.user_id = u.id "
        "WHERE ct.edition_id = ? ORDER BY u.full_name",
        (edition_id,),
    ).fetchall()


# ---------------------------------------------------------------- instancias de evaluación

def get_assignment(db, assignment_id: int):
    return db.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()


def edition_assignments(db, edition_id: int, only_active: bool = False):
    q = "SELECT * FROM assignments WHERE edition_id = ?"
    if only_active:
        q += " AND active = 1"
    return db.execute(q + " ORDER BY id", (edition_id,)).fetchall()


def assignment_items(db, assignment_id: int):
    return db.execute(
        "SELECT * FROM assignment_items WHERE assignment_id = ? ORDER BY orden, id",
        (assignment_id,),
    ).fetchall()


def items_puntaje_total(items) -> float:
    return sum(i["puntaje"] for i in items)


def assignment_cfg(db, edicion, assignment) -> dict:
    """Config que consume el LLM: lo propio de la instancia + contexto + lo global.

    Al modelo se le manda la MATERIA, no la edición: la corrección de un TP de
    Álgebra no cambia porque sea la cursada de 2026 o la de 2027, y meter el
    período solo agrega ruido al prompt.
    """
    g = get_config(db)
    items = assignment_items(db, assignment["id"])
    return {
        "curso": edicion["materia"],
        "instancia": assignment["name"],
        "tipo": assignment["tipo"],
        "consigna": assignment["consigna"],
        "rubrica": assignment["rubrica"],
        "respuestas": assignment["respuestas"],
        "items": [
            {
                "n": n,
                "enunciado": i["enunciado"],
                "respuesta": i["respuesta"],
                "opciones": [o for o in (i["opciones"] or "").splitlines() if o.strip()],
                "puntaje": i["puntaje"],
            }
            for n, i in enumerate(items, 1)
        ],
        "puntaje_total": items_puntaje_total(items),
        "prompt_extra": assignment["prompt_extra"],
        "max_practicas": str(assignment["max_practicas"]),
        "max_preguntas": str(assignment["max_preguntas"]),
        "enviar_nombre": g["enviar_nombre"],
        "banner_deshabilitado": g["banner_deshabilitado"],
    }


# ---------------------------------------------------------------- inscripciones

def student_editions(db, user_id: int):
    """Ediciones en las que está inscripto un estudiante, la más reciente primero.

    Incluye las cerradas: quien terminó la cursada tiene que poder releer sus
    devoluciones. El bloqueo de entregas nuevas se hace al entregar, no acá.
    """
    return db.execute(
        EDICION_SELECT + " JOIN enrollments e ON e.edition_id = ed.id "
        "WHERE e.user_id = ? ORDER BY c.name, ed.created_at DESC",
        (user_id,),
    ).fetchall()


def is_enrolled(db, user_id: int, edition_id: int) -> bool:
    return bool(db.execute(
        "SELECT 1 FROM enrollments WHERE user_id = ? AND edition_id = ?", (user_id, edition_id)
    ).fetchone())


def enroll(db, user_id: int, edition_id: int) -> bool:
    """Inscribe si no estaba. Devuelve True si creó la inscripción."""
    if is_enrolled(db, user_id, edition_id):
        return False
    db.execute(
        "INSERT INTO enrollments (edition_id, user_id, created_at) VALUES (?, ?, ?)",
        (edition_id, user_id, utcnow()),
    )
    return True


# ---------------------------------------------------------------- cupos y estado

def practicas_usadas(db, user_id: int, assignment_id: int) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM submissions WHERE user_id = ? AND assignment_id = ? "
        "AND kind = 'practica' AND status = 'ok'",
        (user_id, assignment_id),
    ).fetchone()
    return row["n"]


def preguntas_usadas(db, submission_id: int) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM questions WHERE submission_id = ?", (submission_id,)
    ).fetchone()
    return row["n"]


def final_activa(db, user_id: int, assignment_id: int):
    """Entrega final vigente (pendiente o aprobada) en la instancia. Las reabiertas no cuentan."""
    return db.execute(
        "SELECT * FROM submissions WHERE user_id = ? AND assignment_id = ? AND kind = 'final' "
        "AND status IN ('pendiente', 'aprobada') ORDER BY id DESC LIMIT 1",
        (user_id, assignment_id),
    ).fetchone()
