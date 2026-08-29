"""El documento original de cada entrega, guardado tal como lo subieron.

La corrección trabaja sobre el texto extraído, y el docente firma leyendo ese texto. Si la
extracción se equivocó —una tabla que se desarmó, una figura que no está, un PDF raro— no
había forma de darse cuenta: el archivo se leía, se convertía y se descartaba.

Se guardan en disco y no en la base a propósito. Un trabajo final pesa entre 2 y 15 MB;
una cursada entera adentro del `.db` lo vuelve imposible de copiar o respaldar, que es algo
que hoy se hace a mano y seguido. En la base queda la referencia, y acá los bytes.

Consecuencia a tener presente: el respaldo pasa a ser dos cosas. Como todo vive bajo
`DATA_DIR`, copiar ese directorio entero alcanza para llevarse la base y los archivos.
"""
import hashlib
import os
import re
import shutil

from .db import DATA_DIR

RAIZ = os.path.join(DATA_DIR, "entregas")

# Un nombre de archivo lo elige quien sube: puede traer barras, «..», o caracteres que
# signifiquen algo para el sistema. El que se guarda en disco lo elegimos nosotros; el
# original queda en la base, para mostrarlo tal cual al descargarlo.
SEGURO = re.compile(r"[^A-Za-z0-9._-]+")
LARGO_MAX = 80


def _carpeta(submission_id: int) -> str:
    return os.path.join(RAIZ, str(int(submission_id)))


def nombre_seguro(nombre: str) -> str:
    base = os.path.basename(nombre or "").strip() or "entrega"
    base = SEGURO.sub("_", base).strip("._-") or "entrega"
    if len(base) > LARGO_MAX:
        raiz, ext = os.path.splitext(base)
        base = raiz[: LARGO_MAX - len(ext)] + ext
    return base


def guardar(submission_id: int, nombre: str, datos: bytes) -> dict:
    """Guarda el archivo de una entrega. Devuelve {ruta, nombre, bytes, sha256}.

    La ruta que se devuelve es relativa a DATA_DIR: guardar la absoluta ataría la base al
    servidor donde se creó, y esta base se copia entre máquinas.
    """
    carpeta = _carpeta(submission_id)
    os.makedirs(carpeta, exist_ok=True)
    seguro = nombre_seguro(nombre)
    destino = os.path.join(carpeta, seguro)
    with open(destino, "wb") as f:
        f.write(datos)
    return {"ruta": os.path.relpath(destino, DATA_DIR), "nombre": seguro,
            "bytes": len(datos), "sha256": hashlib.sha256(datos).hexdigest()}


def ruta_absoluta(relativa: str) -> str | None:
    """La ruta en disco de un archivo guardado, o None si no está o si se salió del corral.

    La comprobación del prefijo no es paranoia: la ruta viene de la base, y si algún día
    algo escribe ahí un valor con «..», esto es lo único que impide leer cualquier archivo
    del servidor.
    """
    if not relativa:
        return None
    completa = os.path.realpath(os.path.join(DATA_DIR, relativa))
    if not completa.startswith(os.path.realpath(RAIZ) + os.sep):
        return None
    return completa if os.path.exists(completa) else None


def borrar(submission_id: int) -> None:
    """Borra los archivos de una entrega. Se usa al eliminar la entrega."""
    shutil.rmtree(_carpeta(submission_id), ignore_errors=True)


def limpiar_huerfanos() -> int:
    """Borra las carpetas cuya entrega ya no existe. Devuelve cuántas sacó.

    Las entregas se borran por cascada —al eliminar una instancia, una cursada o una
    persona— y ninguno de esos caminos pasa por acá. En vez de perseguir cada uno, se
    revisa al arrancar: lo que quedó sin dueño se va. Es una operación barata, son unas
    pocas decenas de carpetas.
    """
    if not os.path.isdir(RAIZ):
        return 0
    from .db import get_db

    with get_db() as db:
        vivas = {str(r["id"]) for r in db.execute("SELECT id FROM submissions")}
    sacadas = 0
    for nombre in os.listdir(RAIZ):
        if nombre.isdigit() and nombre not in vivas:
            shutil.rmtree(os.path.join(RAIZ, nombre), ignore_errors=True)
            sacadas += 1
    return sacadas
