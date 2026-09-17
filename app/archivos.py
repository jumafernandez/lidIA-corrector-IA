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
import json
import os
import re
import secrets
import shutil
import time

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


# ------------------------------------------------------------ hojas de un examen en papel
#
# Entre sacar las fotos y confirmar la entrega hay una pantalla en el medio, y la entrega
# —con su carpeta— todavía no existe. Las fotos esperan acá, con la lectura de cada hoja,
# y al confirmar se mudan a la carpeta de la entrega. Lo que nadie confirma se limpia solo.
PENDIENTES = os.path.join(RAIZ, "_pendientes")
EXTENSION = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
VIDA_PENDIENTE = 24 * 3600


def _pendiente(token: str) -> str:
    # El token lo generamos nosotros, pero termina en una ruta: se filtra igual.
    limpio = SEGURO.sub("", token or "")
    return os.path.join(PENDIENTES, limpio) if limpio and limpio == token else ""


def _escribir_meta(carpeta: str, meta: dict) -> None:
    with open(os.path.join(carpeta, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def preparar_hojas(user_id: int, imagenes: list, textos: list, assignment_id: int, kind: str) -> str:
    """Deja las fotos y su lectura a la espera de la confirmación. Devuelve el token."""
    token = secrets.token_urlsafe(18)
    carpeta = _pendiente(token)
    os.makedirs(carpeta, exist_ok=True)
    hojas = []
    for n, ((mime, datos), texto) in enumerate(zip(imagenes, textos), 1):
        nombre = f"hoja-{n:02d}{EXTENSION.get(mime, '.jpg')}"
        with open(os.path.join(carpeta, nombre), "wb") as f:
            f.write(datos)
        hojas.append({"n": n, "archivo": nombre, "mime": mime, "texto": texto})
    _escribir_meta(carpeta, {"user_id": int(user_id), "assignment_id": int(assignment_id),
                             "kind": kind, "creado": time.time(), "hojas": hojas})
    return token


def hojas_pendientes(token: str, user_id: int) -> dict | None:
    """Lo que espera bajo ese token, si es de esta persona. None si no está o no es suyo."""
    carpeta = _pendiente(token)
    meta_ruta = os.path.join(carpeta, "meta.json") if carpeta else ""
    if not meta_ruta or not os.path.isfile(meta_ruta):
        return None
    with open(meta_ruta, encoding="utf-8") as f:
        meta = json.load(f)
    if meta.get("user_id") != int(user_id):
        return None
    meta["token"] = token
    return meta


def hoja_pendiente(token: str, user_id: int, n: int) -> tuple | None:
    """(ruta absoluta, mime) de una hoja en espera, o None."""
    meta = hojas_pendientes(token, user_id)
    for h in (meta or {}).get("hojas", []):
        if h["n"] == n:
            ruta = os.path.join(_pendiente(token), h["archivo"])
            return (ruta, h["mime"]) if os.path.isfile(ruta) else None
    return None


def reemplazar_hoja(token: str, user_id: int, n: int, mime: str, datos: bytes, texto: str) -> bool:
    """Cambia una hoja en espera por otra foto, con su nueva lectura."""
    meta = hojas_pendientes(token, user_id)
    if not meta:
        return False
    carpeta = _pendiente(token)
    for h in meta["hojas"]:
        if h["n"] == n:
            viejo = os.path.join(carpeta, h["archivo"])
            if os.path.isfile(viejo):
                os.remove(viejo)
            h["archivo"] = f"hoja-{n:02d}{EXTENSION.get(mime, '.jpg')}"
            h["mime"], h["texto"] = mime, texto
            with open(os.path.join(carpeta, h["archivo"]), "wb") as f:
                f.write(datos)
            _escribir_meta(carpeta, meta)
            return True
    return False


def consolidar_hojas(token: str, user_id: int, submission_id: int) -> list:
    """Muda las hojas en espera a la carpeta de la entrega. Devuelve [{n, ruta, mime}]."""
    meta = hojas_pendientes(token, user_id)
    if not meta:
        return []
    origen, destino = _pendiente(token), _carpeta(submission_id)
    os.makedirs(destino, exist_ok=True)
    guardadas = []
    for h in meta["hojas"]:
        de = os.path.join(origen, h["archivo"])
        if not os.path.isfile(de):
            continue
        a = os.path.join(destino, h["archivo"])
        shutil.move(de, a)
        guardadas.append({"n": h["n"], "ruta": os.path.relpath(a, DATA_DIR), "mime": h["mime"]})
    shutil.rmtree(origen, ignore_errors=True)
    return guardadas


def limpiar_pendientes(vida: int = VIDA_PENDIENTE) -> int:
    """Borra las lecturas que nadie confirmó en un día. Devuelve cuántas sacó."""
    if not os.path.isdir(PENDIENTES):
        return 0
    sacadas = 0
    for nombre in os.listdir(PENDIENTES):
        carpeta = os.path.join(PENDIENTES, nombre)
        try:
            if time.time() - os.path.getmtime(carpeta) > vida:
                shutil.rmtree(carpeta, ignore_errors=True)
                sacadas += 1
        except OSError:
            continue
    return sacadas


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
