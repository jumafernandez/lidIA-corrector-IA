"""Freno a los intentos de contraseña, para las dos puertas de LidIA.

Sin esto, un formulario de contraseña se puede probar sin límite. El caso concreto que
lo motivó: la pantalla de vinculación de docentes es alcanzable desde un lanzamiento del
campus, o sea desde afuera. Pero el login de siempre tiene el mismo problema y se arregla
en el mismo lugar.

**El freno nunca le cierra la puerta a quien sabe su contraseña.** Esto es deliberado y
es la corrección de un error propio: en la primera versión el freno corría ANTES de
verificar la clave, así que cualquiera que supiera un DNI —y los DNI circulan— podía
dejar a esa persona quince minutos afuera de su propia cuenta, repitiéndolo para siempre.
Un candado que un tercero puede cerrar desde afuera no es una defensa, es un ataque.

Ahora la contraseña se verifica primero: si es correcta, se entra, punto. El contador solo
frena intentos FALLIDOS, que es lo que hace inviable adivinar sin poder bloquear a nadie.
Queda además un tope alto que corta antes de calcular el hash, para que nadie use el
formulario como bomba de CPU; ese tope está tan por encima del uso normal que ninguna
persona real lo alcanza.

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
TOPE_USUARIO = 8       # fallos sobre la misma cuenta: a partir de acá no se acierta más
TOPE_ABUSO = 60        # fallos sobre la misma cuenta antes de ni siquiera calcular el hash
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


def abuso(login: str) -> bool:
    """Tope duro, para cortar antes de gastar CPU en el hash. Muy por encima del uso real."""
    with get_db() as db:
        _limpiar(db)
        return _fallos_de(db, login) >= TOPE_ABUSO


def bloqueado(login: str, ip: str) -> str:
    """Motivo del freno, o cadena vacía. Se consulta DESPUÉS de fallar la contraseña.

    Nunca se llama antes de verificar la clave: hacerlo permitiría que un tercero deje
    afuera a alguien con solo saber su usuario.
    """
    with get_db() as db:
        _limpiar(db)
        frenado = _fallos_de(db, login) >= TOPE_USUARIO
        cuentas = _cuentas_desde(db, ip)
    if cuentas >= AVISO_CUENTAS:
        log.warning("posible barrido de contraseñas: %s cuentas distintas fallando desde %s",
                    cuentas, ip)
    if frenado:
        return ("Demasiados intentos con ese usuario. Esperá unos minutos y volvé a "
                "probar, o pedile al equipo docente que te regenere la contraseña.")
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
