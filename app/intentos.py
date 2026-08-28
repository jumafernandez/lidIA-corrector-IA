"""Freno a los intentos de contraseña, para las dos puertas de LidIA.

Sin esto, un formulario de contraseña se puede probar sin límite. El caso concreto que
lo motivó: la pantalla de vinculación de docentes es alcanzable desde un lanzamiento del
campus, o sea desde afuera. Pero el login de siempre tiene el mismo problema y se arregla
en el mismo lugar.

**El freno real es por cuenta**: ocho intentos fallidos sobre el mismo usuario y esa
cuenta queda en pausa quince minutos. Eso es lo que hace inviable adivinar una contraseña,
y no depende de dónde venga el intento.

Por origen NO se bloquea, y es una decisión deliberada. La idea era frenar a quien barre
muchos usuarios desde una misma conexión, pero un aula entera detrás de la salida a
internet de la facultad se ve igual: treinta personas equivocándose son treinta cuentas
distintas desde una IP. Lo único que las diferencia es que la clase termina entrando, y
eso se sabe después. Bloquear ahí dejaría a un curso afuera en medio de un parcial, que
es mucho peor que el ataque que evitaría — sobre todo cuando el tope por cuenta ya limita
a ocho intentos por usuario, venga de donde venga. Así que el origen se registra y se
avisa en el log, para que quede rastro si alguna vez hay que mirarlo.

Los intentos exitosos limpian el contador: quien se equivocó dos veces y acertó no
arrastra nada.
"""
import logging
import time

from .db import get_db

log = logging.getLogger("lidia.intentos")

VENTANA = 15 * 60      # los intentos se olvidan a los 15 minutos
TOPE_USUARIO = 8       # intentos fallidos sobre la misma cuenta
AVISO_CUENTAS = 15     # cuentas distintas desde un origen: no bloquea, deja rastro


def init_intentos_db():
    with get_db() as db:
        # La primera versión de esta tabla guardaba una sola columna 'clave'. Nunca se
        # desplegó, pero si quedó dando vueltas en algún entorno de desarrollo se rehace:
        # son intentos fallidos de los últimos minutos, no hay nada que conservar.
        cols = {r["name"] for r in db.execute("PRAGMA table_info(intentos)")}
        if cols and "clave" in cols:
            db.execute("DROP TABLE intentos")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS intentos (
            id INTEGER PRIMARY KEY,
            login TEXT NOT NULL,
            origen TEXT NOT NULL,
            cuando INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_intentos_login ON intentos(login, cuando);
        CREATE INDEX IF NOT EXISTS idx_intentos_origen ON intentos(origen, cuando);
        """)


def _limpiar(db):
    db.execute("DELETE FROM intentos WHERE cuando < ?", (int(time.time()) - VENTANA,))


def _fallos_de(db, login: str) -> int:
    return db.execute(
        "SELECT COUNT(*) n FROM intentos WHERE login = ? AND cuando >= ?",
        (login, int(time.time()) - VENTANA),
    ).fetchone()["n"]


def _cuentas_desde(db, ip: str) -> int:
    """Cuántas cuentas DISTINTAS se probaron desde ese origen. Eso delata el barrido."""
    return db.execute(
        "SELECT COUNT(DISTINCT login) n FROM intentos WHERE origen = ? AND cuando >= ?",
        (ip, int(time.time()) - VENTANA),
    ).fetchone()["n"]


def origen(request) -> str:
    """De dónde viene. Detrás del proxy de la universidad la real va en X-Forwarded-For."""
    reenviado = request.headers.get("x-forwarded-for", "")
    if reenviado:
        return reenviado.split(",")[0].strip()
    return request.client.host if request.client else "?"


def bloqueado(login: str, ip: str) -> str:
    """Devuelve el motivo si hay que frenar, o cadena vacía si puede intentar."""
    with get_db() as db:
        _limpiar(db)
        if _fallos_de(db, login) >= TOPE_USUARIO:
            return ("Demasiados intentos con ese usuario. Esperá unos minutos y volvé a "
                    "probar, o pedile al equipo docente que te regenere la contraseña.")
        cuentas = _cuentas_desde(db, ip)
    if cuentas >= AVISO_CUENTAS:
        log.warning("posible barrido de contraseñas: %s cuentas distintas fallando desde %s",
                    cuentas, ip)
    return ""


def fallo(login: str, ip: str):
    with get_db() as db:
        db.execute("INSERT INTO intentos (login, origen, cuando) VALUES (?, ?, ?)",
                   (login, ip, int(time.time())))


def acierto(login: str, ip: str):
    """Al entrar bien se limpia esa cuenta: equivocarse y acertar no deja rastro.

    El origen NO se limpia: si alguien acierta una de veinte cuentas que viene probando,
    justamente no es el momento de perdonarle el barrido.
    """
    with get_db() as db:
        db.execute("DELETE FROM intentos WHERE login = ?", (login,))
