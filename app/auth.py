"""Autenticación: hash de contraseñas y sesiones por cookie."""
import hashlib
import secrets

from fastapi import Request

from .db import get_db, utcnow

COOKIE_NAME = "lidia_session"

# Cookie aparte para las sesiones que entran desde el campus. Existe porque el ingreso
# por LTI es una navegación entre sitios y necesita SameSite=None, mientras que la cookie
# normal se queda en Lax, que hoy es la única defensa de LidIA contra CSRF. Separarlas
# deja el login por contraseña exactamente como estaba.
LTI_COOKIE_NAME = "lidia_lti"
_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS)
    return f"pbkdf2${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS)
        return secrets.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def generate_password() -> str:
    """Contraseña inicial legible, ej. dia-k3xw7q."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "dia-" + "".join(secrets.choice(alphabet) for _ in range(6))


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with get_db() as db:
        db.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, utcnow()),
        )
    return token


def destroy_session(token: str):
    with get_db() as db:
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))


def cookie_lti() -> dict:
    """Atributos de la cookie de sesión que llega desde el campus."""
    return {
        "httponly": True, "secure": True, "samesite": "none",
        "max_age": 60 * 60 * 12,
    }


def current_user(request: Request):
    token = request.cookies.get(COOKIE_NAME) or request.cookies.get(LTI_COOKIE_NAME)
    if not token:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
    return row
