"""Base de datos SQLite: esquema, conexión y configuración."""
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "lidia.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    -- El apellido y el nombre van separados porque son dos datos, no uno: por el apellido
    -- se ordena y se arman las actas, y pegarlos obliga a adivinar después dónde cortar.
    -- «Ana Suárez Pérez» no se puede partir bien sin saber cuál es cuál.
    apellido TEXT NOT NULL DEFAULT '',
    nombre TEXT NOT NULL DEFAULT '',
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
    -- El año va aparte de la etiqueta: la etiqueta es texto libre («2C», «Verano»,
    -- «Comisión A») y no hay forma confiable de leerle un año adentro.
    anio INTEGER NOT NULL DEFAULT 0,
    etiqueta TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,     -- inactiva = cursada cerrada (solo lectura)
    created_at TEXT NOT NULL,
    -- La etiqueta se repite entre años: «1C» existe todos los años. Lo que no se puede
    -- repetir es la combinación de materia, año y etiqueta.
    UNIQUE (course_id, anio, etiqueta)
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
    max_integrantes INTEGER NOT NULL DEFAULT 1,  -- 1 = individual; >1 habilita grupos
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

-- Grupos de TP. Existen dentro de una instancia: el mismo par de personas que
-- hace juntas el TP1 puede no hacer junto el TP2.
CREATE TABLE IF NOT EXISTS grupos (
    id INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

-- assignment_id se repite acá a propósito: es lo que permite exigir por esquema
-- que nadie esté en dos grupos de la misma instancia.
CREATE TABLE IF NOT EXISTS grupo_miembros (
    grupo_id INTEGER NOT NULL REFERENCES grupos(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    PRIMARY KEY (grupo_id, user_id),
    UNIQUE (assignment_id, user_id)
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

-- Lo que alguien lleva escrito de un examen que se rinde en la plataforma. Vive en el
-- servidor y no en el navegador a propósito: un corte de luz o una pestaña cerrada no
-- pueden costar el examen, y quien se queda sin máquina sigue desde otra donde estaba.
CREATE TABLE IF NOT EXISTS borradores (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    respuestas TEXT NOT NULL DEFAULT '{}',   -- JSON {numero de pregunta: respuesta}
    iniciado_at TEXT NOT NULL,
    guardado_at TEXT NOT NULL,
    UNIQUE (user_id, assignment_id)
);

-- Lo que pasó mientras alguien rendía: que se fue de la pantalla, que pegó texto. No
-- bloquea nada —una página web no puede sellar la máquina y no vamos a fingir que sí—:
-- se registra, se le avisa a quien rinde en el momento, y lo ve el equipo docente junto
-- a la entrega. Es una señal para que la mire una persona, no un veredicto.
CREATE TABLE IF NOT EXISTS incidentes (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id) ON DELETE CASCADE,
    submission_id INTEGER REFERENCES submissions(id) ON DELETE CASCADE,
    tipo TEXT NOT NULL,                      -- 'salida' | 'pegado'
    detalle TEXT NOT NULL DEFAULT '{}',      -- JSON: segundos afuera, caracteres pegados
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidentes_entrega ON incidentes(submission_id);
CREATE INDEX IF NOT EXISTS idx_incidentes_persona ON incidentes(user_id, assignment_id);
"""

# Configuración global (transversal). La consigna, rúbrica, indicaciones y cupos
# viven en cada instancia de evaluación.
DEFAULT_CONFIG = {
    "enviar_nombre": "1",  # enviar nombre de pila del estudiante al modelo
    # Si los docentes pueden crear materias y cursadas por su cuenta. Van juntas a
    # propósito: una materia sin cursada no sirve para nada, así que abrir una sin la
    # otra dejaría al docente a mitad de camino y pidiéndole igual a la coordinación.
    "docentes_crean_materias": "1",
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


def nombre_completo(apellido: str, nombre: str) -> str:
    """Cómo se escribe un nombre en pantalla: «Apellido, Nombre».

    Se arma en un solo lugar para que todas las pantallas lo escriban igual, y se guarda
    en `full_name` para que nada de lo que ya lee ese campo tenga que enterarse.
    """
    apellido, nombre = (apellido or "").strip(), (nombre or "").strip()
    if apellido and nombre:
        return f"{apellido}, {nombre}"
    return apellido or nombre


def partir_nombre(completo: str) -> tuple[str, str]:
    """(apellido, nombre) a partir de un nombre escrito todo junto.

    Corta SOLO donde hay una coma, porque esa coma la escribió una persona que sabía cuál
    era cuál. Sin coma no se adivina: «Ana Suárez Pérez» puede ser un nombre con dos
    apellidos o dos nombres con uno, y elegir mal ordena mal para siempre y sin que se
    note. En ese caso va todo al apellido: el listado queda alfabético y el error a la
    vista de quien pueda corregirlo, que es mejor que un orden equivocado en silencio.
    """
    completo = (completo or "").strip()
    if "," in completo:
        apellido, nombre = completo.split(",", 1)
        return apellido.strip(), nombre.strip()
    return completo, ""


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


def _reconstruir_ediciones():
    """025 — Cambia la restricción de unicidad de las cursadas.

    Venía siendo materia + etiqueta, de cuando la etiqueta cargaba con el año adentro.
    Con el año como campo propio esa regla estorba: «1C» existe todos los años, y la
    combinación que de verdad no se puede repetir es materia + año + etiqueta.

    SQLite no sabe cambiar una restricción de tabla, así que hay que reconstruirla: tabla
    nueva, copiar, borrar la vieja, renombrar. El CREATE de la nueva se saca del de la
    vieja cambiándole solo esa línea, para que no haya forma de que las columnas queden
    distintas. Va en su propia conexión y en autocommit porque `PRAGMA foreign_keys` se
    ignora dentro de una transacción, y sin apagarlas el DROP no se puede hacer.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        actual = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'course_editions'"
        ).fetchone()
        if not actual or "UNIQUE (course_id, etiqueta)" not in actual["sql"]:
            return  # ya está reconstruida, o es una base nueva que nació bien
        columnas = [r["name"] for r in conn.execute("PRAGMA table_info(course_editions)")]
        lista = ", ".join(columnas)
        creacion = (actual["sql"]
                    .replace("UNIQUE (course_id, etiqueta)", "UNIQUE (course_id, anio, etiqueta)")
                    .replace("course_editions", "course_editions_nueva", 1))

        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys = OFF")
        # Cuántas referencias colgadas hay ANTES. Una base vieja puede arrastrar alguna
        # (sesiones de usuarios borrados, por ejemplo) que no tiene nada que ver con esto.
        # Lo que hay que impedir es que la reconstrucción agregue nuevas, no negarse a
        # trabajar porque la base ya venía con las suyas.
        antes = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        conn.execute("BEGIN")
        try:
            conn.execute(creacion)
            conn.execute(f"INSERT INTO course_editions_nueva ({lista})"
                         f" SELECT {lista} FROM course_editions")
            conn.execute("DROP TABLE course_editions")
            conn.execute("ALTER TABLE course_editions_nueva RENAME TO course_editions")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_course_editions_course"
                         " ON course_editions(course_id)")
            despues = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            if despues > antes:
                raise sqlite3.IntegrityError(
                    f"la reconstrucción dejó {despues - antes} referencias rotas nuevas")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()


def _anio_inicial(fila) -> int:
    """El año de una cursada que se creó antes de que el año existiera como campo.

    Por orden de confianza: la fecha de inicio si está cargada, el año que aparezca en
    la etiqueta —que hasta acá era donde se escribía—, y por último el año en que se dio
    de alta la cursada, que es el que menos falla de los que quedan.
    """
    for texto in ((fila["fecha_inicio"] or "").strip(), (fila["etiqueta"] or "").strip(),
                  (fila["created_at"] or "").strip()):
        m = ANIO_EN_TEXTO.search(texto)
        if m:
            return int(m.group(0))
    return anio_actual()


def init_db():
    with get_db() as db:
        db.executescript(SCHEMA)
        # migraciones sobre bases existentes (CREATE IF NOT EXISTS no altera tablas viejas)
        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
        if "avatar" not in cols:
            db.execute("ALTER TABLE users ADD COLUMN avatar BLOB")
            db.execute("ALTER TABLE users ADD COLUMN avatar_mime TEXT DEFAULT ''")
        # 003 — nota de la entrega y trabajos grupales (columnas aditivas)
        cols = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
        if "nota" not in cols:
            db.execute("ALTER TABLE submissions ADD COLUMN nota REAL")
            db.execute("ALTER TABLE submissions ADD COLUMN grupo_id INTEGER REFERENCES grupos(id)")
        cols = {r["name"] for r in db.execute("PRAGMA table_info(assignments)")}
        if "max_integrantes" not in cols:
            # 1 = entrega individual; >1 habilita grupos de hasta ese tamaño
            db.execute("ALTER TABLE assignments ADD COLUMN max_integrantes INTEGER NOT NULL DEFAULT 1")

        # 004 — instrumentación. Todo esto es aditivo y nada de esto se puede
        # reconstruir en retrospectiva: o se registra cuando ocurre, o se perdió.
        cols = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
        if "cfg_snapshot" not in cols:
            # con qué material se corrigió: consigna, rúbrica, ítems y prompt_extra
            # vigentes en ese momento. Sin esto una devolución vieja no es reproducible.
            db.execute("ALTER TABLE submissions ADD COLUMN cfg_snapshot TEXT")
            db.execute("ALTER TABLE submissions ADD COLUMN tokens_in INTEGER")
            db.execute("ALTER TABLE submissions ADD COLUMN tokens_out INTEGER")
            db.execute("ALTER TABLE submissions ADD COLUMN latencia_ms INTEGER")
            db.execute("ALTER TABLE submissions ADD COLUMN finish_reason TEXT")
            # cuánto editó el docente la propuesta de la IA (0 = la firmó tal cual)
            db.execute("ALTER TABLE submissions ADD COLUMN edit_ratio REAL")
            # cuándo la abrió el estudiante por primera vez (¿la leyó?)
            db.execute("ALTER TABLE submissions ADD COLUMN first_viewed_at TEXT")
            # valoración del estudiante: 1 útil / -1 no útil, con comentario opcional
            db.execute("ALTER TABLE submissions ADD COLUMN valoracion INTEGER")
            db.execute("ALTER TABLE submissions ADD COLUMN valoracion_texto TEXT DEFAULT ''")
            db.execute("ALTER TABLE submissions ADD COLUMN valoracion_at TEXT")
        cols = {r["name"] for r in db.execute("PRAGMA table_info(course_editions)")}
        if "fecha_inicio" not in cols:
            # la ventana de la cursada: permite situar cada entrega en el cuatrimestre
            db.execute("ALTER TABLE course_editions ADD COLUMN fecha_inicio TEXT DEFAULT ''")
            db.execute("ALTER TABLE course_editions ADD COLUMN fecha_fin TEXT DEFAULT ''")
        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
        cols = {r["name"] for r in db.execute("PRAGMA table_info(assignments)")}
        if "pide_propuesta" not in cols:
            # 006 — Instancias que se corrigen contra un alcance acordado previamente
            # (típicamente un trabajo final contra su propuesta aprobada). El documento
            # lo sube el estudiante junto con el trabajo; si no lo sube, se corrige igual
            # y queda marcado.
            db.execute("ALTER TABLE assignments ADD COLUMN pide_propuesta INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE submissions ADD COLUMN propuesta_text TEXT DEFAULT ''")
            db.execute("ALTER TABLE submissions ADD COLUMN sin_propuesta INTEGER NOT NULL DEFAULT 0")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(assignments)")}
        if "requiere_revision" not in cols:
            # 007 — Una instancia puede ser solo formativa: N intentos de práctica y nada
            # que firmar. Con revisión (lo normal) son N + 1: las prácticas más la entrega
            # final que revisa y firma una persona.
            db.execute("ALTER TABLE assignments ADD COLUMN requiere_revision INTEGER NOT NULL DEFAULT 1")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(course_editions)")}
        if "programa" not in cols:
            # 005 — El programa es de la CURSADA, no de la materia: cambia entre ediciones
            # (contenidos, bibliografía, equipo docente) y es el contexto que comparten
            # todas las instancias de evaluación de esa cursada.
            db.execute("ALTER TABLE course_editions ADD COLUMN programa TEXT DEFAULT ''")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(course_editions)")}
        if "programa_archivo" not in cols:
            # 008 — El programa entra como documento (el que ya aprobó el Consejo), no se
            # tipea acá: se guarda el texto extraído, que el equipo docente verifica, junto
            # con el nombre del archivo del que salió.
            db.execute("ALTER TABLE course_editions ADD COLUMN programa_archivo TEXT DEFAULT ''")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(assignments)")}
        if "modalidad" not in cols:
            # 010 — Cómo se entrega (digital, foto del papel, o ambas) y la ventana de
            # entrega. Las fechas sí cierran: un plazo que no cierra no es un plazo.
            # Vacías = sin restricción, que es como venía funcionando.
            db.execute(
                "ALTER TABLE assignments ADD COLUMN modalidad TEXT NOT NULL DEFAULT 'ambos'"
                " CHECK (modalidad IN ('digital', 'papel', 'ambos'))"
            )
            db.execute("ALTER TABLE assignments ADD COLUMN fecha_apertura TEXT DEFAULT ''")
            db.execute("ALTER TABLE assignments ADD COLUMN fecha_cierre TEXT DEFAULT ''")
            # un trabajo abierto se entrega digital salvo que alguien diga lo contrario
            db.execute("UPDATE assignments SET modalidad = 'digital' WHERE tipo = 'abierto'")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
        if "cargada_por" not in cols:
            # 009 — El examen en papel lo puede subir el equipo docente por el estudiante
            # (mesa de examen, alguien sin teléfono, una hoja que llegó en mano). Queda
            # registrado quién la subió: la entrega es del estudiante, la carga no.
            db.execute("ALTER TABLE submissions ADD COLUMN cargada_por INTEGER REFERENCES users(id)")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
        if "consent_at" not in cols:
            # consentimiento para usar entregas anonimizadas en investigación educativa
            db.execute("ALTER TABLE users ADD COLUMN consent_at TEXT")
            db.execute("ALTER TABLE users ADD COLUMN consent INTEGER")
        cols = {r["name"] for r in db.execute("PRAGMA table_info(courses)")}
        if "creado_por" not in cols:
            # 011 — Quién dio de alta la materia. Hace falta desde que un docente puede
            # crearlas: recién creada no tiene ninguna cursada, y sin este dato no habría
            # forma de distinguir «la mía, todavía vacía» de «la de cualquier otro».
            # NULL = las que ya existían, todas de coordinación.
            db.execute("ALTER TABLE courses ADD COLUMN creado_por INTEGER REFERENCES users(id)")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
        if "initial_password" in cols:
            # 012 — La contraseña en claro se elimina. Cada persona fija la suya con un
            # enlace por correo, así que no hay nada que repartir ni que guardar. Se borra
            # la columna entera y no solo su contenido: mientras exista, algo va a volver
            # a escribirla. Quien ya tenía contraseña la conserva —el hash no se toca—.
            db.execute("ALTER TABLE users DROP COLUMN initial_password")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(assignments)")}
        if "pide_repo" not in cols:
            # 013 — En los trabajos con código el informe remite a un repositorio, y sin
            # mirarlo no se puede saber si lo que el informe afirma es cierto. El enlace se
            # pide en la entrega, no se busca dentro del documento.
            db.execute("ALTER TABLE assignments ADD COLUMN pide_repo INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
        if "repo_url" not in cols:
            db.execute("ALTER TABLE submissions ADD COLUMN repo_url TEXT DEFAULT ''")
            # Qué se leyó del repositorio en el momento de corregir. El repositorio sigue
            # cambiando después; sin esto, la devolución deja de ser reproducible.
            db.execute("ALTER TABLE submissions ADD COLUMN repo_resumen TEXT DEFAULT ''")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(enrollments)")}
        if "active" not in cols:
            # 014 — Habilitar o no a alguien es una decisión de una cursada, no de la
            # persona: quien la toma es su docente, y no tiene por qué alcanzar a las
            # cursadas de otros. `users.active` sigue existiendo con otro significado —la
            # cuenta entera, que es de coordinación—, y son cosas distintas a propósito.
            db.execute("ALTER TABLE enrollments ADD COLUMN active INTEGER NOT NULL DEFAULT 1")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
        if "alerta" not in cols:
            # 015 — Si el material entregado traía órdenes dirigidas al corrector, queda
            # anotado acá para que el docente lo vea al firmar. Es un dato de la entrega y
            # no un estado: sirve tanto para avisar como para poder contar después cuántas
            # veces pasó, que es algo que nadie mide y este sistema puede.
            db.execute("ALTER TABLE submissions ADD COLUMN alerta TEXT DEFAULT ''")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
        if "promovida_de" not in cols:
            # 016 — Una final puede nacer de una práctica que el estudiante decidió
            # presentar tal cual, sin volver a corregir. Se guarda de cuál salió y no se
            # muta la práctica: el historial de intentos es dato del proceso, y perderlo
            # para ahorrar una fila sería perder justamente lo que se quiere estudiar.
            db.execute("ALTER TABLE submissions ADD COLUMN promovida_de INTEGER"
                       " REFERENCES submissions(id)")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
        if "niveles" not in cols:
            # 017 — El nivel alcanzado en cada criterio de la rúbrica. Ya estaba en la
            # devolución, pero en prosa: guardado como dato se puede mostrar de un vistazo
            # y, sobre todo, se puede medir —si un criterio mejora entre una entrega y la
            # siguiente, o si la firma del docente coincide con lo que la IA había marcado—.
            db.execute("ALTER TABLE submissions ADD COLUMN niveles TEXT DEFAULT ''")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
        if "imagenes" not in cols:
            # 018 — Cuántas imágenes traía el archivo entregado. La corrección es sobre el
            # texto, así que un gráfico o un esquema no llegan al modelo; contarlas permite
            # decirlo en vez de que el docente lo descubra por una devolución que no habla
            # de la figura donde estaba la evidencia principal del trabajo.
            db.execute("ALTER TABLE submissions ADD COLUMN imagenes INTEGER NOT NULL DEFAULT 0")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(assignments)")}
        if "usa_vision" not in cols:
            # 019 — La instancia se corrige mirando las páginas en vez de leyendo el texto
            # extraído. Sirve cuando la evidencia está en figuras y tablas; cuesta más y
            # obliga a entregar en PDF, así que es una decisión del docente y no un default.
            db.execute("ALTER TABLE assignments ADD COLUMN usa_vision INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
        if "paginas" not in cols:
            # Cuántas páginas tenía el trabajo y cuántas se llegaron a mirar.
            db.execute("ALTER TABLE submissions ADD COLUMN paginas INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE submissions ADD COLUMN paginas_vistas INTEGER NOT NULL DEFAULT 0")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
        if "archivo_ruta" not in cols:
            # 020 — El documento original de la entrega. Hasta acá se leía, se convertía a
            # texto y se descartaba: si la extracción se equivocaba, nadie podía notarlo
            # porque no quedaba contra qué comparar. Los bytes van a disco bajo DATA_DIR;
            # esto es solo la referencia.
            db.execute("ALTER TABLE submissions ADD COLUMN archivo_ruta TEXT DEFAULT ''")
            db.execute("ALTER TABLE submissions ADD COLUMN archivo_bytes INTEGER NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE submissions ADD COLUMN archivo_sha256 TEXT DEFAULT ''")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(submissions)")}
        if "detalle_nota" not in cols:
            # 021 — De dónde salió la calificación: los niveles por criterio en un trabajo
            # abierto, los puntos por pregunta en un examen. Sin esto la nota es un número
            # y no se puede discutir; con esto se muestra la cuenta.
            db.execute("ALTER TABLE submissions ADD COLUMN detalle_nota TEXT DEFAULT ''")

        # 022 — Los cupos de una instancia los fija su tipo de evaluación: un examen se
        # rinde una vez, sin versiones previas ni repreguntas. Las instancias creadas
        # antes de esa regla habían nacido con los tres intentos del molde del trabajo
        # abierto, y quedaban ofreciendo versiones que el resto del sistema ya no admite.
        #
        # No lleva marca de aplicada a propósito: toca solo las filas fuera del rango
        # vigente. Si mañana un tipo admite más, esto deja de tocarlas solo, en vez de
        # volver a pisar lo que el equipo docente configuró.
        from .circuito import CUPOS
        for tipo, campos in CUPOS.items():
            for campo, columna in (("practicas", "max_practicas"), ("preguntas", "max_preguntas")):
                minimo, maximo, _ = campos[campo]
                db.execute(
                    f"UPDATE assignments SET {columna} = max(?, min(?, {columna}))"
                    f" WHERE tipo = ? AND ({columna} < ? OR {columna} > ?)",
                    (minimo, maximo, tipo, minimo, maximo),
                )

        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
        if "panel_filtro" not in cols:
            # 023 — Qué filtro dejó puesto cada persona en su espacio: el año en curso,
            # los anteriores o todas. Se guarda el modo elegido y nunca un año concreto:
            # guardar «2026» haría que en 2027 la persona entrara mirando el año pasado.
            # Va por persona y no en la sesión porque tener que volver a elegirlo en cada
            # ingreso convierte un filtro en una molestia. Vacío = todavía no eligió.
            db.execute("ALTER TABLE users ADD COLUMN panel_filtro TEXT NOT NULL DEFAULT ''")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(course_editions)")}
        if "anio" not in cols:
            # 024 — El año de la cursada, como dato propio. Hasta acá el año vivía dentro
            # de la etiqueta, y la etiqueta es texto libre: puede decir «2C», «Verano» o
            # «Comisión A». Leerle un año adentro funciona hasta que alguien escribe otra
            # cosa. Con el año aparte, la etiqueta queda libre para lo que realmente es.
            db.execute("ALTER TABLE course_editions ADD COLUMN anio INTEGER NOT NULL DEFAULT 0")

        # El relleno vive fuera del `if`: año 0 es «sin clasificar», venga de la migración
        # o de una carga que lo dejó vacío. Normalmente no hay ninguna y esto no hace nada.
        for fila in db.execute(
            "SELECT id, etiqueta, fecha_inicio, created_at FROM course_editions WHERE anio = 0"
        ).fetchall():
            db.execute("UPDATE course_editions SET anio = ? WHERE id = ?",
                       (_anio_inicial(fila), fila["id"]))

        cols = {r["name"] for r in db.execute("PRAGMA table_info(assignments)")}
        if "en_plataforma" not in cols:
            # 026 — La instancia se rinde ACÁ: el estudiantado escribe o marca en un
            # espacio de examen, con reloj, guardado en el servidor y registro de lo que
            # pasa mientras rinde. Sin esto, el examen se desarrolla afuera y lo único
            # que ve el sistema es el archivo que llega al final.
            db.execute("ALTER TABLE assignments ADD COLUMN en_plataforma INTEGER NOT NULL DEFAULT 0")

        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
        if "apellido" not in cols:
            # 027 — El apellido y el nombre, separados. Hasta acá vivían pegados en
            # `full_name` con la convención «Apellido, Nombre», que se sostenía sola
            # mientras los cargara a mano alguien que la conociera. Cualquier alta que
            # viniera del campus llegaba sin coma y había que adivinar el corte.
            db.execute("ALTER TABLE users ADD COLUMN apellido TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE users ADD COLUMN nombre TEXT NOT NULL DEFAULT ''")

        # Relleno de las filas que todavía no lo tengan. Fuera del `if` porque una fila sin
        # apellido es una fila sin clasificar, venga de donde venga.
        for fila in db.execute(
            "SELECT id, full_name FROM users WHERE TRIM(apellido) = '' AND TRIM(full_name) != ''"
        ).fetchall():
            apellido, nombre = partir_nombre(fila["full_name"])
            db.execute("UPDATE users SET apellido = ?, nombre = ? WHERE id = ?",
                       (apellido, nombre, fila["id"]))

        # Sesiones de usuarios que ya no existen. La clave foránea las borraría sola, pero
        # las que quedaron son de antes de que existiera, o de un borrado hecho con las
        # claves apagadas. No molestan, salvo que ensucian cualquier revisión de integridad
        # que se haga después: una comprobación que siempre da resultados deja de servir
        # para avisar cuando pasa algo de verdad.
        sueltas = db.execute(
            "DELETE FROM sessions WHERE user_id NOT IN (SELECT id FROM users)").rowcount
        if sueltas:
            logging.getLogger("lidia").info(
                "Se borraron %s sesiones de usuarios que ya no existen.", sueltas)

        for key, value in DEFAULT_CONFIG.items():
            db.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (key, value))

    # Fuera del bloque de arriba: necesita que `anio` exista y esté rellenado, y maneja
    # su propia conexión porque apaga las claves foráneas mientras reconstruye.
    #
    # Si falla, se avisa y se sigue. La reconstrucción revierte sola, así que el peor caso
    # es quedarse con la restricción vieja —que funciona, solo es más estricta de lo que
    # queremos—. Dejar la aplicación sin arrancar por esto sería mucho peor que el problema
    # que resuelve: ya pasó una vez, en producción.
    try:
        _reconstruir_ediciones()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("lidia").error(
            "no se pudo cambiar la restricción de unicidad de las cursadas: %s", exc)


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


def visible_courses(db, user):
    """Materias que este usuario ve en el listado.

    Coordinación, todas. Un docente ve las que le tocan: aquellas con alguna cursada suya,
    más las que creó él —incluida la que acaba de crear, que todavía no tiene ninguna—. El
    resto del catálogo no es asunto suyo: expone qué se dicta, con qué docentes y con
    cuántos estudiantes.
    """
    if user["role"] == "admin":
        return all_courses(db)
    return db.execute(
        "SELECT DISTINCT c.* FROM courses c"
        " LEFT JOIN course_editions ed ON ed.course_id = c.id"
        " LEFT JOIN course_teachers ct ON ct.edition_id = ed.id AND ct.user_id = ?"
        " WHERE ct.user_id IS NOT NULL OR c.creado_por = ?"
        " ORDER BY c.name", (user["id"], user["id"]),
    ).fetchall()


def all_courses(db, only_active: bool = False):
    q = "SELECT * FROM courses"
    if only_active:
        q += " WHERE active = 1"
    return db.execute(q + " ORDER BY name").fetchall()


# ---------------------------------------------------------------- ediciones
#
# `nombre` es el nombre completo que se muestra en pantalla («Álgebra 2026 2C»);
# se arma en SQL para que las plantillas no tengan que componerlo en cada lugar.

def periodo_sql(alias: str = "ed") -> str:
    """Cómo se lee un período, listo para pegar en un SELECT que use ese alias.

    El año, y la etiqueta al lado solo cuando dice algo más que el año. Sin esto, una
    cursada con etiqueta «2026» y año 2026 se leería «2026 2026»: mientras la etiqueta
    siga siendo el año en las cursadas viejas, el nombre tiene que aguantarlo.
    """
    return (f"CASE WHEN TRIM({alias}.etiqueta) IN ('', CAST({alias}.anio AS TEXT))"
            f" THEN CAST({alias}.anio AS TEXT)"
            f" ELSE CAST({alias}.anio AS TEXT) || ' ' || {alias}.etiqueta END")


_PERIODO = periodo_sql("ed")

EDICION_SELECT = f"""
SELECT ed.*, c.name AS materia, c.active AS materia_active,
       {_PERIODO} AS periodo,
       c.name || ' ' || {_PERIODO} AS nombre,
       c.name || ' ' || {_PERIODO} AS name
  FROM course_editions ed JOIN courses c ON c.id = ed.course_id
"""


def get_edition(db, edition_id: int):
    return db.execute(EDICION_SELECT + " WHERE ed.id = ?", (edition_id,)).fetchone()


def course_editions(db, course_id: int):
    """Ediciones de una materia, de la más reciente a la más vieja."""
    return db.execute(
        EDICION_SELECT + " WHERE ed.course_id = ? ORDER BY ed.anio DESC, ed.id DESC",
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
    orden = " ORDER BY c.name, ed.anio DESC, ed.etiqueta"
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
        "WHERE ct.edition_id = ? ORDER BY u.apellido, u.nombre",
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


AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def ventana_entrega(assignment) -> tuple:
    """(abierta, motivo). Las fechas vacías significan sin restricción."""
    if "fecha_apertura" not in assignment.keys():
        return True, ""
    ahora = datetime.now(AR_TZ).strftime("%Y-%m-%dT%H:%M")
    desde = _momento((assignment["fecha_apertura"] or "").strip(), inicio=True)
    hasta = _momento((assignment["fecha_cierre"] or "").strip(), inicio=False)
    if desde and ahora < desde:
        return False, f"Esta instancia abre el {fecha_corta(assignment['fecha_apertura'])}."
    if hasta and ahora > hasta:
        return False, f"El plazo de entrega venció el {fecha_corta(assignment['fecha_cierre'])}."
    return True, ""


def momento_apertura(assignment) -> str:
    """Cuándo abre, como «YYYY-MM-DDTHH:MM». Vacío = sin restricción."""
    return _momento((assignment["fecha_apertura"] or "").strip(), inicio=True)


def momento_cierre(assignment) -> str:
    """Cuándo cierra, como «YYYY-MM-DDTHH:MM». Vacío = sin plazo.

    Lo usa el reloj del examen en plataforma: la cuenta regresiva se ancla a esto y al
    reloj del servidor, nunca al de la máquina de quien rinde, que puede estar corrido.
    """
    return _momento((assignment["fecha_cierre"] or "").strip(), inicio=False)


def ahora_local() -> str:
    """El momento actual en el mismo formato que las fechas de las instancias."""
    return datetime.now(AR_TZ).strftime("%Y-%m-%dT%H:%M")


def _momento(valor: str, inicio: bool) -> str:
    """Lleva la fecha guardada a «YYYY-MM-DDTHH:MM» para poder comparar como texto.

    Una fecha sin hora significa el día entero: abre a las 00:00 y cierra al terminar el
    día. Es lo que ya hacía antes de que existiera la hora, así que las instancias
    cargadas hasta ahora siguen comportándose igual.
    """
    if not valor:
        return ""
    return valor if "T" in valor else valor + ("T00:00" if inicio else "T23:59")


def fecha_corta(iso: str) -> str:
    """2026-09-08 → 8/9/2026; 2026-09-08T14:30 → 8/9/2026 14:30.

    La hora se muestra solo si está: una entrega domiciliaria se anuncia por día y decir
    «vence el 8/9/2026 a las 00:00» confundiría más de lo que informa.
    """
    try:
        dia, _, hora = (iso or "").partition("T")
        a, m, d = dia.split("-")
        salida = f"{int(d)}/{int(m)}/{a}"
        return f"{salida} {hora[:5]}" if hora else salida
    except (ValueError, AttributeError):
        return iso


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
        "programa": (edicion["programa"] or "") if "programa" in edicion.keys() else "",
        "instancia": assignment["name"],
        "pide_propuesta": bool(assignment["pide_propuesta"]) if "pide_propuesta" in assignment.keys() else False,
        "usa_vision": bool(assignment["usa_vision"]) if "usa_vision" in assignment.keys() else False,
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
        "WHERE e.user_id = ? ORDER BY ed.anio DESC, c.name",
        (user_id,),
    ).fetchall()


ANIO_EN_TEXTO = re.compile(r"(19|20)\d{2}")


def anio_actual() -> int:
    """El año en curso en Buenos Aires: es el que se muestra por defecto."""
    return datetime.now(AR_TZ).year


def is_enrolled(db, user_id: int, edition_id: int) -> bool:
    """¿Está inscripto? Sirve para VER: sus entregas y devoluciones siguen siendo suyas.

    Deliberadamente no mira si la inscripción está habilitada. A alguien deshabilitado se
    le corta presentar cosas nuevas, no el acceso a lo que ya hizo —igual que con una
    cursada cerrada—. Para eso está `inscripcion_habilitada`.
    """
    return bool(db.execute(
        "SELECT 1 FROM enrollments WHERE user_id = ? AND edition_id = ?", (user_id, edition_id)
    ).fetchone())


def inscripcion_habilitada(db, user_id: int, edition_id: int) -> bool:
    """¿Puede presentar en esta cursada? Es por cursada, no por persona."""
    fila = db.execute(
        "SELECT active FROM enrollments WHERE user_id = ? AND edition_id = ?",
        (user_id, edition_id),
    ).fetchone()
    return bool(fila and fila["active"])


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

# ---------------------------------------------------------------- grupos

def grupo_de(db, user_id: int, assignment_id: int):
    """El grupo de esta persona en esta instancia, o None si entrega sola."""
    return db.execute(
        "SELECT g.* FROM grupos g JOIN grupo_miembros m ON m.grupo_id = g.id "
        "WHERE m.user_id = ? AND m.assignment_id = ?",
        (user_id, assignment_id),
    ).fetchone()


def miembros_de(db, grupo_id: int):
    return db.execute(
        "SELECT u.* FROM users u JOIN grupo_miembros m ON m.user_id = u.id "
        "WHERE m.grupo_id = ? ORDER BY u.apellido, u.nombre",
        (grupo_id,),
    ).fetchall()


def grupos_de_instancia(db, assignment_id: int):
    return db.execute(
        "SELECT * FROM grupos WHERE assignment_id = ? ORDER BY id", (assignment_id,)
    ).fetchall()


def _ids_del_grupo(db, user_id: int, assignment_id: int) -> list:
    """user_ids cuyas entregas cuentan como del mismo autor: el grupo, o la persona sola."""
    g = grupo_de(db, user_id, assignment_id)
    if not g:
        return [user_id]
    return [m["id"] for m in miembros_de(db, g["id"])]


# ---------------------------------------------------------------- cupos y estado

def practicas_usadas(db, user_id: int, assignment_id: int) -> int:
    """Cupo consumido. En un TP grupal, el cupo es del grupo, no de cada integrante."""
    ids = _ids_del_grupo(db, user_id, assignment_id)
    marks = ",".join("?" * len(ids))
    row = db.execute(
        f"SELECT COUNT(*) AS n FROM submissions WHERE user_id IN ({marks}) AND assignment_id = ? "
        "AND kind = 'practica' AND status = 'ok'",
        [*ids, assignment_id],
    ).fetchone()
    return row["n"]


def preguntas_usadas(db, submission_id: int) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM questions WHERE submission_id = ?", (submission_id,)
    ).fetchone()
    return row["n"]


def final_activa(db, user_id: int, assignment_id: int):
    """Entrega final vigente (pendiente o aprobada) en la instancia. Las reabiertas no cuentan.

    En un TP grupal, la final de cualquier integrante es la del grupo.
    """
    ids = _ids_del_grupo(db, user_id, assignment_id)
    marks = ",".join("?" * len(ids))
    return db.execute(
        f"SELECT * FROM submissions WHERE user_id IN ({marks}) AND assignment_id = ? AND kind = 'final' "
        "AND status IN ('pendiente', 'aprobada') ORDER BY id DESC LIMIT 1",
        [*ids, assignment_id],
    ).fetchone()
