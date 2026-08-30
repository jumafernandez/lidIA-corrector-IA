"""El examen que se rinde acá adentro.

Hasta ahora una instancia se desarrollaba afuera —en Word, en papel, donde fuera— y el
sistema veía únicamente el archivo que llegaba al final. Cuando la instancia se marca
«en plataforma», el desarrollo pasa a ocurrir en una pantalla propia: se escribe o se
marca ahí, lo escrito se guarda en el servidor mientras se trabaja, y la entrega se arma
con eso.

Dos decisiones que conviene tener presentes al leer esto:

El borrador vive en el servidor y no en el navegador. Un examen de dos horas no puede
perderse porque se cortó la luz, se cerró una pestaña o se quedó sin batería: quien vuelve
—desde esa máquina o desde otra— encuentra lo que había escrito. Y lo que vale al cerrar
la ventana es lo que estaba guardado, no lo que quedó en una pantalla que nadie envió.

Los incidentes se registran, no bloquean. Una página web no puede impedir que alguien
cambie de aplicación, abra el teléfono o copie de otro lado; prometer lo contrario sería
vender una seguridad que no existe. Lo que sí se puede es dejar constancia de lo que
ocurre en la pantalla del examen, avisárselo a quien rinde en el momento, y mostrárselo
después a quien corrige. Es una señal para que la mire una persona, nunca un veredicto
automático.
"""
import json

from .db import get_db, utcnow

# Tipos de incidente que se registran. Deliberadamente pocos y concretos: cada uno tiene
# que poder explicarse en una frase a quien rinde y a quien corrige.
TIPOS = {
    "salida": "Salió de la pantalla del examen",
    "pegado": "Pegó texto desde otro lado",
}

# Cuánto puede seguir guardándose después de la hora de cierre. Es para la carrera entre
# el reloj y el último guardado —se escribe hasta el final y el envío tarda—, no para dar
# tiempo extra: el reloj del navegador y el del servidor nunca coinciden al segundo.
GRACIA_SEGUNDOS = 120


def borrador(db, user_id: int, assignment_id: int):
    return db.execute(
        "SELECT * FROM borradores WHERE user_id = ? AND assignment_id = ?",
        (user_id, assignment_id),
    ).fetchone()


def abrir(db, user_id: int, assignment_id: int):
    """El borrador de esta persona, creándolo si es la primera vez que entra.

    `iniciado_at` queda fijo desde acá: es cuándo empezó a rendir, y con eso la ficha
    puede decir cuánto tardó.
    """
    fila = borrador(db, user_id, assignment_id)
    if fila:
        return fila
    ahora = utcnow()
    db.execute(
        "INSERT INTO borradores (user_id, assignment_id, respuestas, iniciado_at, guardado_at)"
        " VALUES (?, ?, '{}', ?, ?)", (user_id, assignment_id, ahora, ahora),
    )
    return borrador(db, user_id, assignment_id)


def guardar(db, user_id: int, assignment_id: int, respuestas: dict):
    """Guarda lo escrito hasta ahora. No crea el borrador: si no existe, no hay examen abierto."""
    db.execute(
        "UPDATE borradores SET respuestas = ?, guardado_at = ?"
        " WHERE user_id = ? AND assignment_id = ?",
        (json.dumps(respuestas, ensure_ascii=False), utcnow(), user_id, assignment_id),
    )


def respuestas_de(fila) -> dict:
    """Las respuestas guardadas, con las claves como enteros. {} si no hay nada."""
    if not fila:
        return {}
    try:
        crudo = json.loads(fila["respuestas"] or "{}")
    except ValueError:
        return {}
    salida = {}
    for k, v in (crudo or {}).items():
        try:
            salida[int(k)] = v
        except (TypeError, ValueError):
            continue
    return salida


def borrar(db, user_id: int, assignment_id: int):
    db.execute("DELETE FROM borradores WHERE user_id = ? AND assignment_id = ?",
               (user_id, assignment_id))


# ------------------------------------------------------------------ incidentes

def registrar(db, user_id: int, assignment_id: int, tipo: str, detalle: dict) -> int:
    """Deja constancia de un incidente y devuelve cuántos van de ese tipo.

    El número vuelve a la pantalla para poder decirle a quien rinde «es la tercera vez»:
    avisar sin decir cuántas van invita a pensar que no se está contando.
    """
    if tipo not in TIPOS:
        return 0
    db.execute(
        "INSERT INTO incidentes (user_id, assignment_id, tipo, detalle, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (user_id, assignment_id, tipo, json.dumps(detalle, ensure_ascii=False), utcnow()),
    )
    return db.execute(
        "SELECT COUNT(*) n FROM incidentes WHERE user_id = ? AND assignment_id = ? AND tipo = ?",
        (user_id, assignment_id, tipo),
    ).fetchone()["n"]


def colgar_de_entrega(db, user_id: int, assignment_id: int, submission_id: int):
    """Ata a la entrega los incidentes que todavía no eran de ninguna."""
    db.execute(
        "UPDATE incidentes SET submission_id = ?"
        " WHERE user_id = ? AND assignment_id = ? AND submission_id IS NULL",
        (submission_id, user_id, assignment_id),
    )


def de_entrega(db, submission_id: int):
    return db.execute(
        "SELECT * FROM incidentes WHERE submission_id = ? ORDER BY created_at",
        (submission_id,),
    ).fetchall()


def resumen(filas) -> dict:
    """Los incidentes contados y en una frase, para la ficha de la entrega."""
    salidas = [f for f in filas if f["tipo"] == "salida"]
    pegados = [f for f in filas if f["tipo"] == "pegado"]
    segundos = sum(_dato(f, "segundos") for f in salidas)
    caracteres = sum(_dato(f, "caracteres") for f in pegados)
    partes = []
    if salidas:
        partes.append(f"Salió de la pantalla {_veces(len(salidas))}"
                      + (f" ({_duracion(segundos)} en total)" if segundos else ""))
    if pegados:
        partes.append(f"Pegó texto {_veces(len(pegados))}"
                      + (f" ({caracteres} caracteres)" if caracteres else ""))
    return {"hay": bool(filas), "salidas": len(salidas), "pegados": len(pegados),
            "segundos": segundos, "caracteres": caracteres,
            "frase": " · ".join(partes) or "Sin incidentes registrados."}


def _dato(fila, clave: str) -> int:
    try:
        return int(json.loads(fila["detalle"] or "{}").get(clave, 0) or 0)
    except (ValueError, TypeError):
        return 0


def _veces(n: int) -> str:
    return "1 vez" if n == 1 else f"{n} veces"


def _duracion(segundos: int) -> str:
    if segundos < 60:
        return f"{segundos} s"
    minutos, resto = divmod(segundos, 60)
    return f"{minutos} min" + (f" {resto} s" if resto else "")


# ------------------------------------------------------- de lo guardado a la entrega

def texto_de(items, respuestas: dict) -> str:
    """Las respuestas escritas, numeradas como las espera la corrección del escrito.

    El formato es el mismo que si lo hubiera entregado en un archivo —«1. …», «2. …»—,
    así la puntuación por pregunta funciona igual sin saber por dónde entró la entrega.
    """
    partes = []
    for it in items:
        texto = (respuestas.get(it["orden"]) or "").strip()
        partes.append(f"{it['orden']}. " + (texto if texto else "(sin responder)"))
    return "\n\n".join(partes)


def marcadas_de(items, respuestas: dict) -> dict:
    """Las opciones elegidas, como las manda el formulario de marcado del panel."""
    salida = {}
    for it in items:
        letra = str(respuestas.get(it["orden"]) or "").strip().lower()[:1]
        if letra:
            salida[it["orden"]] = letra
    return salida


def hay_algo(items, respuestas: dict, tipo: str) -> bool:
    """¿Alcanza para entregar? Una hoja en blanco no se entrega sola al vencer el plazo."""
    if tipo == "choice":
        return bool(marcadas_de(items, respuestas))
    return any((respuestas.get(it["orden"]) or "").strip() for it in items)
