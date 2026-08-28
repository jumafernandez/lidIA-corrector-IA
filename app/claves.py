"""Cada persona elige su propia contraseña.

Antes el sistema generaba una clave, la guardaba en claro y alguien la repartía. Eso tiene
tres problemas: la contraseña existe escrita en algún lado, viaja por donde sea que se la
mandaron, y queda igual para siempre en la cuenta de quien no la cambia. Nadie más que su
dueño tiene por qué conocer una contraseña, ni siquiera la coordinación.

En su lugar se manda un enlace de un solo uso al correo de la persona, y con ese enlace
ella fija la suya. Sirve para las dos situaciones que existen: el primer ingreso y el
olvido. El enlace se guarda hasheado, así que ver la base tampoco alcanza para entrar.
"""
import hashlib
import secrets
import time

from .db import get_db, utcnow

# El alta se manda antes de que empiece la cursada y puede quedar sin abrir unos días; el
# olvido lo pide alguien que está intentando entrar ahora mismo y no tiene por qué esperar.
VIGENCIA = {"alta": 7 * 24 * 3600, "olvido": 2 * 3600}

# Cuántos enlaces se le pueden pedir a una misma cuenta antes de dejar de mandar. No es
# para proteger la cuenta —el enlace va al correo de su dueño— sino para que nadie use
# LidIA como máquina de llenarle la casilla a otro.
TOPE_POR_CUENTA = 5
VENTANA_TOPE = 3600


def init_claves_db():
    """Crea la tabla de enlaces. Idempotente, se llama al arrancar."""
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS clave_enlaces (
            id INTEGER PRIMARY KEY,
            hash TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            motivo TEXT NOT NULL,
            expira_en INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            usado_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_clave_user ON clave_enlaces(user_id);
        """)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def limpiar_vencidos():
    with get_db() as db:
        db.execute("DELETE FROM clave_enlaces WHERE expira_en < ?", (int(time.time()) - 86400,))


def crear(user_id: int, motivo: str = "olvido") -> str | None:
    """Genera un enlace nuevo y devuelve su token, o None si esta cuenta ya pidió muchos.

    Los anteriores de la misma cuenta se invalidan: si alguien pidió dos porque el primero
    no le llegó, que el que valga sea el último. Devolver None no es un error a mostrar:
    quien pide el enlace ve siempre la misma respuesta, exista o no la cuenta.
    """
    limpiar_vencidos()
    ahora = int(time.time())
    with get_db() as db:
        recientes = db.execute(
            "SELECT COUNT(*) n FROM clave_enlaces WHERE user_id = ? AND expira_en > ?",
            (user_id, ahora + VIGENCIA[motivo] - VENTANA_TOPE),
        ).fetchone()["n"]
        if recientes >= TOPE_POR_CUENTA:
            return None
        db.execute("UPDATE clave_enlaces SET usado_at = ? WHERE user_id = ? AND usado_at IS NULL",
                   (utcnow(), user_id))
        token = secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO clave_enlaces (hash, user_id, motivo, expira_en, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (_hash(token), user_id, motivo, ahora + VIGENCIA[motivo], utcnow()),
        )
    return token


def usuario_de(token: str):
    """El usuario del enlace, si el enlace sirve. None si no existe, venció o ya se usó."""
    if not token:
        return None
    with get_db() as db:
        fila = db.execute(
            "SELECT e.*, u.id AS uid, u.login, u.full_name, u.role FROM clave_enlaces e"
            " JOIN users u ON u.id = e.user_id"
            " WHERE e.hash = ? AND e.usado_at IS NULL AND e.expira_en > ?",
            (_hash(token), int(time.time())),
        ).fetchone()
    return fila


def consumir(token: str, password_hash: str) -> bool:
    """Fija la contraseña y quema el enlace, en una sola operación.

    Va junto a propósito: si se marcara el enlace como usado por un lado y se guardara la
    contraseña por otro, un corte en el medio dejaría a la persona sin contraseña nueva y
    sin enlace para volver a intentar.
    """
    fila = usuario_de(token)
    if not fila:
        return False
    with get_db() as db:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, fila["uid"]))
        db.execute("UPDATE clave_enlaces SET usado_at = ? WHERE id = ?", (utcnow(), fila["id"]))
    return True


def buscar_cuenta(dato: str):
    """La cuenta a la que mandarle el enlace, buscando por DNI/usuario o por correo."""
    dato = (dato or "").strip()
    if not dato:
        return None
    with get_db() as db:
        return db.execute(
            "SELECT * FROM users WHERE (login = ? OR lower(email) = lower(?)) AND active = 1",
            (dato, dato),
        ).fetchone()


def clave_inutilizable() -> str:
    """Un hash que ninguna contraseña produce, para una cuenta que todavía no tiene la suya.

    La cuenta existe y se puede inscribir a una cursada, pero no se entra hasta que su
    dueño usa el enlace. Es preferible a dejar `password_hash` vacío, que según cómo se
    compare podría llegar a validar.
    """
    from . import auth
    return auth.hash_password(secrets.token_urlsafe(48))
