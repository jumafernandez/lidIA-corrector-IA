"""Traer el código que el estudiante enlazó junto con su entrega.

En los trabajos de la diplomatura el informe casi siempre remite a un repositorio: el
código es la mitad del trabajo, y sin él no se puede saber si lo que el informe afirma es
cierto. Esto lo trae para que Lidia lo tenga a la vista.

El enlace se pide explícitamente en la entrega y no se busca dentro del documento. La
diferencia importa: un informe tiene enlaces a todo —la fuente de datos, un paper, un
tutorial— y salir a buscar cualquier dirección que aparezca en un archivo significa que
nuestro servidor visita lo que el estudiante decida. Un campo declarado es acotado, el
docente lo ve, y la instancia decide si lo pide o no.

Lo que se trae es material del estudiante, igual que su informe: entra al prompt marcado
como objeto de evaluación y nunca como instrucciones.
"""
import io
import re
import urllib.error
import urllib.request
import zipfile

# Solo estos servicios. No es una lista de "sitios buenos": es que de estos sabemos bajar
# un repositorio entero por su API pública, sin credenciales y sin ejecutar nada. Cualquier
# otra dirección se rechaza en vez de intentarlo, para que el servidor no termine visitando
# lo que a alguien se le ocurra escribir.
SERVICIOS = {
    "github.com": "https://codeload.github.com/{duenio}/{repo}/zip/refs/heads/{rama}",
    "gitlab.com": "https://gitlab.com/{duenio}/{repo}/-/archive/{rama}/{repo}-{rama}.zip",
}
RAMAS = ("main", "master")

# Qué archivos entran. El código y la documentación, no los datos ni los binarios: un CSV
# de cien mil filas no aporta nada a la corrección y se come todo el presupuesto de texto.
EXTENSIONES = (
    ".py", ".ipynb", ".r", ".sql",                      # lo habitual en un trabajo con datos
    ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rb", ".php", ".sh",
    ".md", ".txt", ".yml", ".yaml", ".toml", ".cfg",    # documentación y configuración
)
IGNORAR = ("/.git/", "/node_modules/", "/venv/", "/.venv/", "/__pycache__/", "/site-packages/",
           "/.ipynb_checkpoints/", "/dist/", "/build/")

MAX_ZIP = 25 * 1024 * 1024      # lo que se acepta bajar
MAX_ARCHIVO = 120_000           # por archivo, antes de recortar
MAX_TOTAL = 60_000              # todo junto: después lo recorta el tope del prompt
MAX_ARCHIVOS = 40
TIMEOUT = 25


class RepoError(Exception):
    """No se pudo traer el repositorio. El mensaje es para quien entrega."""


def normalizar(url: str) -> tuple[str, str, str]:
    """Valida el enlace y devuelve (servicio, dueño, repo). Lanza RepoError si no sirve."""
    url = (url or "").strip()
    if not url:
        raise RepoError("Falta el enlace del repositorio.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    m = re.match(r"^https?://(?:www\.)?([a-z.]+)/([^/\s]+)/([^/\s?#]+)", url, re.IGNORECASE)
    if not m:
        raise RepoError("Ese enlace no parece el de un repositorio.")
    servicio, duenio, repo = m.group(1).lower(), m.group(2), m.group(3)
    if servicio not in SERVICIOS:
        cuales = " o ".join(SERVICIOS)
        raise RepoError(f"Por ahora solo se pueden leer repositorios de {cuales}.")
    return servicio, duenio, repo.removesuffix(".git")


def traer(url: str) -> tuple[str, dict]:
    """Descarga el repositorio y devuelve (texto, resumen).

    `resumen` es lo que se guarda con la entrega: de qué repositorio y qué archivos se
    leyeron. Sin eso la devolución deja de ser reproducible, porque el repositorio sigue
    cambiando después de la corrección y nadie podría saber contra qué se corrigió.
    """
    servicio, duenio, repo = normalizar(url)
    datos = _descargar(servicio, duenio, repo)
    return _leer_zip(datos, f"{duenio}/{repo}")


def _descargar(servicio: str, duenio: str, repo: str) -> bytes:
    """El zip del repositorio, probando las ramas habituales."""
    ultimo = ""
    for rama in RAMAS:
        destino = SERVICIOS[servicio].format(duenio=duenio, repo=repo, rama=rama)
        pedido = urllib.request.Request(destino, headers={"User-Agent": "LidIA/1.0"})
        try:
            with urllib.request.urlopen(pedido, timeout=TIMEOUT) as resp:
                datos = resp.read(MAX_ZIP + 1)
            if len(datos) > MAX_ZIP:
                raise RepoError(f"El repositorio pesa más de {MAX_ZIP // (1024 * 1024)} MB.")
            return datos
        except urllib.error.HTTPError as exc:
            ultimo = "no existe o es privado" if exc.code in (403, 404) else f"error {exc.code}"
        except urllib.error.URLError as exc:
            raise RepoError(f"No se pudo conectar con {servicio}: {exc.reason}") from exc
    raise RepoError(f"No se pudo bajar {duenio}/{repo}: {ultimo}. "
                    "Si es privado, hacelo público o entregá el código en el archivo.")


def _leer_zip(datos: bytes, nombre: str) -> tuple[str, dict]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(datos))
    except zipfile.BadZipFile as exc:
        raise RepoError("Lo que se descargó no es un repositorio válido.") from exc

    elegidos = []
    for info in zf.infolist():
        ruta = "/" + info.filename
        if info.is_dir() or any(x in ruta for x in IGNORAR):
            continue
        if not ruta.lower().endswith(EXTENSIONES):
            continue
        elegidos.append(info)
    # Primero lo que describe el trabajo, después el código: si hay que recortar, que lo
    # que se pierda sea el archivo número cuarenta y no el README.
    elegidos.sort(key=lambda i: (not i.filename.lower().endswith((".md", ".txt")), i.filename))
    elegidos = elegidos[:MAX_ARCHIVOS]

    partes, leidos, total = [], [], 0
    for info in elegidos:
        if total >= MAX_TOTAL:
            break
        try:
            crudo = zf.read(info)[:MAX_ARCHIVO]
        except Exception:  # noqa: BLE001
            continue
        texto = _texto_de(info.filename, crudo)
        if not texto.strip():
            continue
        relativa = "/".join(info.filename.split("/")[1:]) or info.filename
        disponible = MAX_TOTAL - total
        if len(texto) > disponible:
            texto = texto[:disponible] + "\n… (recortado)"
        partes.append(f"### {relativa}\n{texto}")
        leidos.append(relativa)
        total += len(texto)

    if not partes:
        raise RepoError("El repositorio no tiene archivos de código o documentación legibles.")
    resumen = {"repo": nombre, "archivos": leidos, "caracteres": total,
               "omitidos": max(0, len(elegidos) - len(leidos))}
    return "\n\n".join(partes), resumen


def _texto_de(nombre: str, crudo: bytes) -> str:
    """Texto de un archivo del repositorio. Los notebooks se reducen como los entregados."""
    if nombre.lower().endswith(".ipynb"):
        try:
            from .extract import _from_ipynb
            return _from_ipynb(crudo)
        except Exception:  # noqa: BLE001
            return ""
    try:
        return crudo.decode("utf-8")
    except UnicodeDecodeError:
        return crudo.decode("latin-1", errors="replace")


def resumen_legible(resumen: dict) -> str:
    """Una línea para mostrarle al docente qué se leyó."""
    n = len(resumen.get("archivos", []))
    extra = f" (+{resumen['omitidos']} sin leer)" if resumen.get("omitidos") else ""
    return (f"{resumen.get('repo', '')} · {n} archivo{'s' if n != 1 else ''}"
            f"{extra} · {resumen.get('caracteres', 0):,} caracteres".replace(",", "."))
