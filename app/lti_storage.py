"""Persistencia de la integración LTI: estado del apretón de manos y vínculos.

Todo vive en la misma base que el resto de LidIA y en tablas propias con prefijo `lti_`,
así que la integración se puede quitar borrando estas tablas sin tocar nada más.

Por qué el estado va en base y no en cookie: el lanzamiento llega como POST entre sitios
y las cookies `SameSite=Lax` no viajan en eso. pylti1p3 igual quiere una cookie para el
`state`, y se la damos (forzada a `SameSite=None; Secure`), pero el `nonce` y los datos
del lanzamiento se guardan acá, donde además podemos hacer que el nonce sea de un solo uso
—que es lo que impide reproducir un lanzamiento robado— cosa que la librería no hace sola.
"""
import json
import time

from .db import get_db, utcnow

TTL_SEGUNDOS = 600  # un lanzamiento que tarda más de 10 minutos no es un lanzamiento


def init_lti_db():
    """Crea las tablas de LTI. Idempotente, se llama al arrancar."""
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS lti_kv (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL,
            expira_en INTEGER NOT NULL
        );

        -- Quién es en LidIA quien llega desde una plataforma. La clave es la terna que
        -- identifica al usuario de forma estable: el DNI solo se usa la primera vez.
        CREATE TABLE IF NOT EXISTS lti_identidades (
            id INTEGER PRIMARY KEY,
            iss TEXT NOT NULL,
            client_id TEXT NOT NULL,
            deployment_id TEXT NOT NULL,
            sub TEXT NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            dni_visto TEXT DEFAULT '',
            nombre_visto TEXT DEFAULT '',
            email_visto TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            visto_at TEXT NOT NULL,
            UNIQUE (iss, client_id, deployment_id, sub)
        );

        -- Qué cursada de LidIA corresponde a qué curso de la plataforma. Lo crea el
        -- docente al elegir en el selector, y sirve para que nadie pueda apuntar la
        -- actividad a una instancia de otra cursada editando el parámetro a mano.
        CREATE TABLE IF NOT EXISTS lti_vinculos (
            id INTEGER PRIMARY KEY,
            iss TEXT NOT NULL,
            client_id TEXT NOT NULL,
            deployment_id TEXT NOT NULL,
            context_id TEXT NOT NULL,
            edition_id INTEGER NOT NULL REFERENCES course_editions(id) ON DELETE CASCADE,
            creado_por INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL,
            UNIQUE (iss, client_id, deployment_id, context_id)
        );

        -- Dónde llamar a los servicios del campus. Las direcciones llegan en cada
        -- lanzamiento; se guardan porque las llamadas se hacen después, de servidor a
        -- servidor, cuando ya no hay ningún lanzamiento a mano.
        CREATE TABLE IF NOT EXISTS lti_servicios (
            id INTEGER PRIMARY KEY,
            iss TEXT NOT NULL,
            client_id TEXT NOT NULL,
            deployment_id TEXT NOT NULL,
            context_id TEXT NOT NULL,
            resource_link_id TEXT NOT NULL DEFAULT '',
            assignment_id INTEGER REFERENCES assignments(id) ON DELETE CASCADE,
            nrps_url TEXT DEFAULT '',
            lineitem_url TEXT DEFAULT '',
            ambitos TEXT DEFAULT '',
            visto_at TEXT NOT NULL,
            UNIQUE (iss, client_id, deployment_id, context_id, resource_link_id)
        );

        CREATE INDEX IF NOT EXISTS idx_lti_kv_expira ON lti_kv(expira_en);
        CREATE INDEX IF NOT EXISTS idx_lti_serv_asig ON lti_servicios(assignment_id);
        """)


def limpiar_vencidos():
    with get_db() as db:
        db.execute("DELETE FROM lti_kv WHERE expira_en < ?", (int(time.time()),))


_clase_almacen = None


def AlmacenSqlite():
    """Almacén de pylti1p3 sobre la base de LidIA.

    Es una fábrica y no una clase por una razón concreta: hereda de `LaunchDataStorage`,
    que vive en pylti1p3, y esa librería es opcional. Definir la clase arriba obligaría a
    tenerla instalada para que LidIA arranque. Así, la clase se construye la primera vez
    que alguien la usa —o sea, solo si hay integración con un campus—.

    La librería la usa para el nonce, el state y los datos del lanzamiento. Guardarlo acá
    en lugar de en la sesión del framework tiene dos ventajas: sobrevive al POST entre
    sitios, donde la cookie de sesión no llega, y nos deja borrar el nonce al leerlo, que
    es la defensa contra reproducción.
    """
    global _clase_almacen
    if _clase_almacen is None:
        from pylti1p3.launch_data_storage.base import LaunchDataStorage

        class _AlmacenSqlite(LaunchDataStorage):
            """`get_session_cookie_name` devuelve None a propósito: sin identificador de
            sesión, `_prepare_key` usa la clave tal cual, que ya es única por lanzamiento
            porque incluye el state. Una cookie más sería una cookie más que puede no llegar.
            """

            def get_session_cookie_name(self):
                return None

            def can_set_keys_expiration(self) -> bool:
                return True

            def get_value(self, key: str):
                k = self._prepare_key(key)
                limpiar_vencidos()
                with get_db() as db:
                    fila = db.execute(
                        "SELECT v FROM lti_kv WHERE k = ? AND expira_en >= ?", (k, int(time.time()))
                    ).fetchone()
                if not fila:
                    return None
                valor = json.loads(fila["v"])
                # El nonce es de un solo uso: reproducir un lanzamiento capturado tiene que fallar.
                # pylti1p3 lo verifica pero no lo consume, así que lo consumimos acá.
                if "nonce" in k:
                    with get_db() as db:
                        db.execute("DELETE FROM lti_kv WHERE k = ?", (k,))
                return valor

            def set_value(self, key: str, value, exp=None) -> None:
                k = self._prepare_key(key)
                vence = int(time.time()) + int(exp or TTL_SEGUNDOS)
                with get_db() as db:
                    db.execute(
                        "INSERT INTO lti_kv (k, v, expira_en) VALUES (?, ?, ?)"
                        " ON CONFLICT(k) DO UPDATE SET v = excluded.v, expira_en = excluded.expira_en",
                        (k, json.dumps(value), vence),
                    )

            def check_value(self, key: str) -> bool:
                k = self._prepare_key(key)
                limpiar_vencidos()
                with get_db() as db:
                    fila = db.execute(
                        "SELECT 1 FROM lti_kv WHERE k = ? AND expira_en >= ?", (k, int(time.time()))
                    ).fetchone()
                if fila and "nonce" in k:
                    with get_db() as db:
                        db.execute("DELETE FROM lti_kv WHERE k = ?", (k,))
                return bool(fila)

        _clase_almacen = _AlmacenSqlite
    return _clase_almacen()


# ------------------------------------------------------------------ identidades

def identidad(iss: str, client_id: str, deployment_id: str, sub: str):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM lti_identidades WHERE iss = ? AND client_id = ?"
            " AND deployment_id = ? AND sub = ?",
            (iss, client_id, deployment_id, sub),
        ).fetchone()


def vincular_identidad(iss, client_id, deployment_id, sub, user_id, dni="", nombre="", email=""):
    with get_db() as db:
        db.execute(
            "INSERT INTO lti_identidades (iss, client_id, deployment_id, sub, user_id,"
            " dni_visto, nombre_visto, email_visto, created_at, visto_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(iss, client_id, deployment_id, sub) DO UPDATE SET"
            "   user_id = excluded.user_id, dni_visto = excluded.dni_visto,"
            "   nombre_visto = excluded.nombre_visto, email_visto = excluded.email_visto,"
            "   visto_at = excluded.visto_at",
            (iss, client_id, deployment_id, sub, user_id, dni, nombre, email, utcnow(), utcnow()),
        )


def marcar_visto(identidad_id: int):
    with get_db() as db:
        db.execute("UPDATE lti_identidades SET visto_at = ? WHERE id = ?", (utcnow(), identidad_id))


# ------------------------------------------------------------------ vínculos de curso

def vinculo(iss: str, client_id: str, deployment_id: str, context_id: str):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM lti_vinculos WHERE iss = ? AND client_id = ?"
            " AND deployment_id = ? AND context_id = ?",
            (iss, client_id, deployment_id, context_id),
        ).fetchone()


def vincular_curso(iss, client_id, deployment_id, context_id, edition_id, creado_por):
    with get_db() as db:
        db.execute(
            "INSERT INTO lti_vinculos (iss, client_id, deployment_id, context_id, edition_id,"
            " creado_por, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(iss, client_id, deployment_id, context_id) DO UPDATE SET"
            "   edition_id = excluded.edition_id, creado_por = excluded.creado_por",
            (iss, client_id, deployment_id, context_id, edition_id, creado_por, utcnow()),
        )


# ------------------------------------------------------------------ extremos de servicio

def guardar_servicios(iss, client_id, deployment_id, context_id, resource_link_id,
                      assignment_id, nrps_url, lineitem_url, ambitos):
    """Anota dónde llamar a los servicios del campus para este curso y esta actividad."""
    if not (nrps_url or lineitem_url):
        return
    with get_db() as db:
        db.execute(
            "INSERT INTO lti_servicios (iss, client_id, deployment_id, context_id,"
            " resource_link_id, assignment_id, nrps_url, lineitem_url, ambitos, visto_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(iss, client_id, deployment_id, context_id, resource_link_id)"
            " DO UPDATE SET assignment_id = COALESCE(excluded.assignment_id, assignment_id),"
            "   nrps_url = CASE WHEN excluded.nrps_url != '' THEN excluded.nrps_url ELSE nrps_url END,"
            "   lineitem_url = CASE WHEN excluded.lineitem_url != '' THEN excluded.lineitem_url"
            "                       ELSE lineitem_url END,"
            "   ambitos = excluded.ambitos, visto_at = excluded.visto_at",
            (iss, client_id, deployment_id, context_id, resource_link_id, assignment_id,
             nrps_url, lineitem_url, ambitos, utcnow()),
        )


def servicios_de_cursada(edition_id: int):
    """El extremo de lista para una cursada: sirve cualquiera de sus actividades."""
    with get_db() as db:
        return db.execute(
            "SELECT s.* FROM lti_servicios s JOIN lti_vinculos v"
            "   ON v.iss = s.iss AND v.client_id = s.client_id"
            "  AND v.deployment_id = s.deployment_id AND v.context_id = s.context_id"
            " WHERE v.edition_id = ? AND COALESCE(s.nrps_url, '') != ''"
            " ORDER BY s.visto_at DESC LIMIT 1", (edition_id,),
        ).fetchone()


def servicios_de_instancia(assignment_id: int):
    """El extremo de notas para una instancia."""
    with get_db() as db:
        return db.execute(
            "SELECT * FROM lti_servicios WHERE assignment_id = ?"
            " AND COALESCE(lineitem_url, '') != '' ORDER BY visto_at DESC LIMIT 1",
            (assignment_id,),
        ).fetchone()
