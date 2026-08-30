"""Llamadas de LidIA al campus: la lista de estudiantes y las notas de vuelta.

A diferencia del lanzamiento, que lo mueve el navegador del estudiante, esto es de
servidor a servidor. LidIA se identifica firmando con su clave privada, el campus le
devuelve un token de acceso, y con ese token pide o escribe.

Dos cosas aprendidas probando contra Moodle 4.5 que conviene no olvidar:

- La nota se manda en TU escala (`scoreMaximum`) y el campus la lleva a la de su
  columna. Un 8,5 sobre 10 aparece como 85 sobre 100 si la columna es centesimal.

- Si la actividad del campus NO tiene calificación numérica configurada, Moodle acepta
  la nota con un 200 alegre y la descarta en silencio. Y consultar la columna no sirve
  para detectarlo: informa el mismo scoreMaximum en los dos casos. Lo único que
  distingue un caso del otro es leer el resultado de vuelta (ver quedo_registrada).
"""
import json
import logging
import pathlib
import time
import uuid
from datetime import datetime, timezone

import jwt as pyjwt
import requests

log = logging.getLogger("lidia.lti.servicios")

AMBITO_LISTA = "https://purl.imsglobal.org/spec/lti-nrps/scope/contextmembership.readonly"
AMBITO_NOTA = "https://purl.imsglobal.org/spec/lti-ags/scope/score"
AMBITO_COLUMNA = "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem"
AMBITO_COLUMNA_LEER = "https://purl.imsglobal.org/spec/lti-ags/scope/lineitem.readonly"

TIEMPO_ESPERA = 20
_tokens: dict = {}   # (iss, client_id, ámbitos) → (token, vence_en)


class ErrorServicio(Exception):
    """Algo del campus no respondió como se esperaba. El mensaje es para mostrar."""


def _plataforma(iss: str) -> dict:
    from .lti import ARCHIVO_PLATAFORMAS
    datos = json.loads(pathlib.Path(ARCHIVO_PLATAFORMAS).read_text()).get(iss)
    if not datos:
        raise ErrorServicio(f"El campus «{iss}» no está registrado en LidIA.")
    return datos if isinstance(datos, dict) else datos[0]


def token(iss: str, *ambitos: str) -> str:
    """Token de acceso del campus, reutilizado mientras siga vivo."""
    from .lti import CLAVE_PRIVADA
    clave = (iss, tuple(sorted(ambitos)))
    guardado = _tokens.get(clave)
    if guardado and guardado[1] > time.time() + 30:
        return guardado[0]

    p = _plataforma(iss)
    ahora = int(time.time())
    afirmacion = pyjwt.encode({
        "iss": p["client_id"], "sub": p["client_id"], "aud": p["auth_token_url"],
        "iat": ahora, "exp": ahora + 60, "jti": uuid.uuid4().hex,
    }, pathlib.Path(CLAVE_PRIVADA).read_text(), algorithm="RS256")

    try:
        r = requests.post(p["auth_token_url"], timeout=TIEMPO_ESPERA, data={
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": afirmacion,
            "scope": " ".join(ambitos),
        })
    except requests.RequestException as exc:
        raise ErrorServicio(f"No se pudo contactar al campus: {exc}") from exc
    if r.status_code != 200:
        raise ErrorServicio(f"El campus no dio permiso ({r.status_code}). "
                            "Puede que los servicios no estén habilitados en la herramienta.")
    datos = r.json()
    if "access_token" not in datos:
        raise ErrorServicio(f"El campus no devolvió un token: {datos}")
    _tokens[clave] = (datos["access_token"], time.time() + int(datos.get("expires_in", 3600)))
    return datos["access_token"]


# ------------------------------------------------------------------ la lista

ROL_ESTUDIANTE = "Learner"
ROLES_DOCENTE = ("Instructor", "Administrator", "ContentDeveloper", "TeachingAssistant")


def lista(iss: str, url: str) -> list:
    """Miembros del curso del campus. Devuelve dicts normalizados."""
    t = token(iss, AMBITO_LISTA)
    gente, siguiente, vueltas = [], url, 0
    while siguiente and vueltas < 20:       # tope: la lista viene paginada
        vueltas += 1
        try:
            r = requests.get(siguiente, timeout=TIEMPO_ESPERA, headers={
                "Authorization": f"Bearer {t}",
                "Accept": "application/vnd.ims.lti-nrps.v2.membershipcontainer+json",
            })
        except requests.RequestException as exc:
            raise ErrorServicio(f"No se pudo leer la lista del campus: {exc}") from exc
        if r.status_code != 200:
            raise ErrorServicio(f"El campus rechazó el pedido de la lista ({r.status_code}).")
        for m in r.json().get("members", []):
            roles = [x.rsplit("#", 1)[-1].rsplit("/", 1)[-1] for x in m.get("roles", [])]
            gente.append({
                "sub": str(m.get("user_id", "")),
                "usuario": (m.get("ext_user_username") or "").strip(),
                "sourcedid": (m.get("lis_person_sourcedid") or "").strip(),
                "nombre": (m.get("name") or "").strip(),
                # El campus manda el apellido y el nombre por separado, y son los buenos:
                # con el nombre completo habría que adivinar dónde cortar, y «Ana Suárez
                # Pérez» no se puede partir sin saber cuál es cuál.
                "apellido": (m.get("family_name") or "").strip(),
                "nombre_pila": (m.get("given_name") or "").strip(),
                "email": (m.get("email") or "").strip(),
                "roles": roles,
                "estudiante": ROL_ESTUDIANTE in roles,
                "docente": any(r_ in ROLES_DOCENTE for r_ in roles),
                "activo": (m.get("status") or "Active") == "Active",
            })
        siguiente = _siguiente_pagina(r)
    return gente


def _siguiente_pagina(r) -> str:
    """La paginación viaja en la cabecera Link, estilo <url>; rel="next"."""
    for parte in (r.headers.get("Link") or "").split(","):
        if 'rel="next"' in parte:
            return parte.split(";")[0].strip().strip("<>")
    return ""


# ------------------------------------------------------------------ las notas

def columna(iss: str, lineitem_url: str) -> dict:
    """La columna del libro de calificaciones, para saber sobre cuánto es y si acepta números."""
    t = token(iss, AMBITO_COLUMNA_LEER, AMBITO_COLUMNA)
    try:
        r = requests.get(lineitem_url, timeout=TIEMPO_ESPERA, headers={
            "Authorization": f"Bearer {t}",
            "Accept": "application/vnd.ims.lis.v2.lineitem+json",
        })
    except requests.RequestException as exc:
        raise ErrorServicio(f"No se pudo leer la columna del campus: {exc}") from exc
    if r.status_code != 200:
        raise ErrorServicio(f"El campus rechazó el pedido de la columna ({r.status_code}).")
    return r.json()


def enviar_nota(iss: str, lineitem_url: str, sub: str, nota: float, sobre: float = 10,
                comentario: str = "") -> str:
    """Manda una nota al libro del campus. Devuelve un detalle para mostrar."""
    destino = lineitem_url.split("?")[0] + "/scores"
    if "?" in lineitem_url:
        destino += "?" + lineitem_url.split("?", 1)[1]
    t = token(iss, AMBITO_NOTA)
    cuerpo = {
        "userId": str(sub),
        "scoreGiven": float(nota),
        "scoreMaximum": float(sobre),
        "activityProgress": "Completed",
        "gradingProgress": "FullyGraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if comentario:
        cuerpo["comment"] = comentario[:1000]
    try:
        r = requests.post(destino, timeout=TIEMPO_ESPERA, json=cuerpo, headers={
            "Authorization": f"Bearer {t}",
            "Content-Type": "application/vnd.ims.lis.v1.score+json",
        })
    except requests.RequestException as exc:
        raise ErrorServicio(f"No se pudo enviar la nota al campus: {exc}") from exc
    if r.status_code == 409:
        raise ErrorServicio("El campus ya tiene una nota más nueva para esa entrega.")
    if r.status_code not in (200, 201, 204):
        raise ErrorServicio(f"El campus rechazó la nota ({r.status_code}): {r.text[:160]}")
    log.info("nota enviada al campus: sub=%s %.2f/%.2f", sub, nota, sobre)
    return f"Nota enviada al campus ({nota:g} de {sobre:g})."


AMBITO_RESULTADO = "https://purl.imsglobal.org/spec/lti-ags/scope/result.readonly"


def quedo_registrada(iss: str, lineitem_url: str, sub: str):
    """¿La nota que acabamos de mandar quedó de verdad en el libro del campus?

    Hay que preguntarlo, no suponerlo. Si la actividad del campus está configurada sin
    calificación numérica, Moodle acepta la nota con un 200 y la descarta: el número
    queda en la base pero el estudiante ve la casilla vacía. Y la columna informa
    `scoreMaximum` igual en los dos casos, así que mirarla no alcanza —lo comprobamos—.

    Lo único que distingue un caso del otro es leer el resultado de vuelta: si la
    columna es de texto, vuelve sin `resultScore`.

    Devuelve (quedó, valor). `quedó` en None significa que no se pudo comprobar, que no
    es lo mismo que haber fallado.
    """
    base = lineitem_url.split("?")[0]
    cola = "?" + lineitem_url.split("?", 1)[1] if "?" in lineitem_url else ""
    try:
        t = token(iss, AMBITO_RESULTADO)
        r = requests.get(f"{base}/results{cola}", timeout=TIEMPO_ESPERA, headers={
            "Authorization": f"Bearer {t}",
            "Accept": "application/vnd.ims.lis.v2.resultcontainer+json",
        })
        if r.status_code != 200:
            return None, None
        for fila in r.json():
            if str(fila.get("userId")) == str(sub):
                if "resultScore" in fila:
                    return True, fila.get("resultScore")
                return False, None
        return None, None
    except (requests.RequestException, ValueError, ErrorServicio):
        return None, None


def resultados(iss: str, lineitem_url: str) -> dict:
    """Todas las notas que hoy tiene esa columna en el campus, por `sub`.

    Una sola llamada trae el curso entero, así que comparar no cuesta casi nada.
    """
    base = lineitem_url.split("?")[0]
    cola = "?" + lineitem_url.split("?", 1)[1] if "?" in lineitem_url else ""
    t = token(iss, AMBITO_RESULTADO)
    try:
        r = requests.get(f"{base}/results{cola}", timeout=TIEMPO_ESPERA, headers={
            "Authorization": f"Bearer {t}",
            "Accept": "application/vnd.ims.lis.v2.resultcontainer+json",
        })
    except requests.RequestException as exc:
        raise ErrorServicio(f"No se pudieron leer las notas del campus: {exc}") from exc
    if r.status_code != 200:
        raise ErrorServicio(f"El campus rechazó el pedido de las notas ({r.status_code}).")
    salida = {}
    for fila in r.json():
        if "resultScore" in fila:
            salida[str(fila.get("userId"))] = {
                "nota": fila["resultScore"],
                "sobre": fila.get("resultMaximum"),
                "comentario": fila.get("comment", ""),
            }
    return salida
