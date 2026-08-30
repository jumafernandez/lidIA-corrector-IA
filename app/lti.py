"""Entrada desde Moodle: el estudiantado llega a LidIA sin escribir contraseña.

El recorrido tiene tres actos. Moodle avisa que alguien quiere entrar (`/lti/login`),
LidIA lo manda a Moodle a que lo firme, y Moodle lo devuelve con un token firmado
(`/lti/launch`) que dice quién es, qué rol tiene y en qué curso está. Con eso LidIA abre
una sesión propia y lo deja adentro de la instancia que corresponde.

Decisiones que valen la pena saber al leer esto:

- La identidad se ata por la terna (emisor, cliente, despliegue, sub) la primera vez, y
  desde entonces esa atadura manda. El DNI solo se usa para el primer encuentro, porque
  es un campo que cualquier administrador de Moodle puede editar y no queremos que
  editarlo cambie de quién es una entrega.

- Sin alta automática: si el DNI que llega no está en LidIA, se muestra un error legible
  en lugar de crear un usuario. LidIA es la fuente de verdad de quién rinde. Se puede
  habilitar con LTI_AUTOALTA=1 cuando se decida.

- La instancia a abrir viene en un parámetro de la actividad, pero se valida que su
  cursada esté vinculada al curso de Moodle desde el que llega: si no, cualquiera que
  edite el parámetro a mano entraría a la instancia de otra materia.
"""
import json
import logging
import os
import pathlib

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth, claves, intentos, lti_storage
from .db import (nombre_completo, can_access_edition, get_assignment, get_db, get_edition,
                 is_enrolled, utcnow)

log = logging.getLogger("lidia.lti")
router = APIRouter(prefix="/lti", tags=["lti"])

RAIZ = pathlib.Path(__file__).resolve().parent.parent
ARCHIVO_PLATAFORMAS = pathlib.Path(
    os.environ.get("LTI_PLATAFORMAS", RAIZ / "data" / "lti" / "plataformas.json"))
CLAVE_PRIVADA = pathlib.Path(os.environ.get("LTI_CLAVE_PRIVADA", RAIZ / "data" / "lti" / "privada.pem"))
CLAVE_PUBLICA = pathlib.Path(os.environ.get("LTI_CLAVE_PUBLICA", RAIZ / "data" / "lti" / "publica.pem"))

CLAIM = "https://purl.imsglobal.org/spec/lti/claim"
CLAIM_NRPS = "https://purl.imsglobal.org/spec/lti-nrps/claim/namesroleservice"
CLAIM_AGS = "https://purl.imsglobal.org/spec/lti-ags/claim/endpoint"
ROL_DOCENTE = ("Instructor", "Administrator", "ContentDeveloper", "TeachingAssistant", "Mentor")

# Destinos legítimos del lanzamiento. Cualquier otro target_link_uri se rechaza: es lo
# que impide que alguien use nuestro /lti/login como redirector abierto.
DESTINOS = ("/lti/launch", "/lti/deeplink")


def libreria_disponible() -> bool:
    """¿Está instalada pylti1p3?

    La integración con el campus es opcional: LidIA tiene que arrancar y funcionar
    igual sin ella. Por eso la librería se importa dentro de las funciones y no arriba:
    un despliegue sin `pip install pylti1p3` levanta lo mismo, solo que /lti/* avisa
    que no está configurado.
    """
    try:
        import pylti1p3  # noqa: F401
        return True
    except ImportError:
        return False


def habilitado() -> bool:
    return (libreria_disponible() and ARCHIVO_PLATAFORMAS.exists()
            and CLAVE_PRIVADA.exists())


def _config():
    """Configuración de plataformas para pylti1p3, con nuestras claves enchufadas."""
    from pylti1p3.tool_config import ToolConfDict
    datos = json.loads(ARCHIVO_PLATAFORMAS.read_text())
    cfg = ToolConfDict(datos)
    privada, publica = CLAVE_PRIVADA.read_text(), CLAVE_PUBLICA.read_text()
    for iss, entradas in datos.items():
        for entrada in (entradas if isinstance(entradas, list) else [entradas]):
            cfg.set_private_key(iss, privada, client_id=entrada["client_id"])
            cfg.set_public_key(iss, publica, client_id=entrada["client_id"])
    return cfg


def _almacen():
    return lti_storage.AlmacenSqlite()


def _error(request: Request, titulo: str, detalle: str, tecnico: str = "", codigo: int = 400):
    """Pantalla de error del lanzamiento. Lo técnico va al log, no a la cara del estudiante."""
    if tecnico:
        log.warning("lanzamiento LTI rechazado: %s | %s", titulo, tecnico)
    from .main import templates  # tardío a propósito: evita la importación circular
    return templates.TemplateResponse(
        request, "lti_error.html", {"titulo": titulo, "detalle": detalle}, status_code=codigo)


# ------------------------------------------------------------------ claves públicas

@router.get("/jwks")
def jwks():
    """Claves públicas de LidIA, para que la plataforma valide lo que firmamos.

    En el laboratorio Moodle usa la clave pegada, pero en producción conviene esto:
    rotar la clave no obliga a reconfigurar la plataforma.
    """
    if not habilitado():
        return {"keys": []}
    return _config().get_jwks()


# ------------------------------------------------------------------ acto 1: aviso

@router.api_route("/login", methods=["GET", "POST"])
async def login(request: Request):
    """Moodle avisa que alguien quiere entrar. Devolvemos una redirección firmada."""
    if not habilitado():
        return _error(request, "La integración no está configurada",
                      "Todavía no se conectó LidIA con el campus. Avisale al equipo docente.",
                      "falta plataformas.json o la clave privada", 503)
    from .lti_starlette import LoginOIDC, PedidoStarlette
    form = await request.form() if request.method == "POST" else None
    pedido = PedidoStarlette(request, form)

    # El destino tiene que ser uno de los nuestros: si no, seríamos un redirector abierto.
    destino = pedido.get_param("target_link_uri") or ""
    if not any(destino.endswith(d) for d in DESTINOS):
        return _error(request, "Destino de lanzamiento no reconocido",
                      "El campus pidió abrir una dirección que LidIA no reconoce.",
                      f"target_link_uri={destino!r}", 400)
    try:
        acceso = LoginOIDC(pedido, _config(), launch_data_storage=_almacen())
        return acceso.enable_check_cookies().redirect(destino)
    except Exception as exc:  # noqa: BLE001
        return _error(request, "No se pudo iniciar la conexión con el campus",
                      "Volvé a entrar desde la actividad en el campus.", repr(exc), 400)


# ------------------------------------------------------------------ acto 2: lanzamiento

async def _validar(request: Request):
    """Valida el token y devuelve (lanzamiento, datos) o (None, respuesta de error)."""
    from .lti_starlette import Lanzamiento, PedidoStarlette
    form = await request.form()
    pedido = PedidoStarlette(request, form)
    try:
        lanzamiento = Lanzamiento(pedido, _config(), launch_data_storage=_almacen())
        lanzamiento.set_launch_data_lifetime(lti_storage.TTL_SEGUNDOS)
        datos = lanzamiento.get_launch_data()
    except Exception as exc:  # noqa: BLE001
        return None, _error(
            request, "No pudimos validar tu ingreso",
            "El enlace pudo haber vencido. Volvé a entrar desde la actividad en el campus.",
            repr(exc), 401)

    # Endurecimiento que pylti1p3 no hace solo.
    iss = datos.get("iss", "")
    aud = datos.get("aud")
    aud = aud if isinstance(aud, list) else [aud]
    esperado = _clientes(iss)
    if not set(aud) & set(esperado):
        return None, _error(request, "Ingreso no válido", "Volvé a intentar desde el campus.",
                            f"aud={aud} no está entre {esperado}", 401)
    if len(aud) > 1 and datos.get("azp") not in esperado:
        return None, _error(request, "Ingreso no válido", "Volvé a intentar desde el campus.",
                            f"azp={datos.get('azp')!r} con aud múltiple", 401)
    despliegue = datos.get(f"{CLAIM}/deployment_id")
    if despliegue not in _despliegues(iss):
        return None, _error(request, "Ingreso no válido", "Volvé a intentar desde el campus.",
                            f"deployment_id={despliegue!r} no habilitado", 401)
    return lanzamiento, datos


def _clientes(iss: str) -> list:
    datos = json.loads(ARCHIVO_PLATAFORMAS.read_text()).get(iss, [])
    return [e["client_id"] for e in (datos if isinstance(datos, list) else [datos])]


def _despliegues(iss: str) -> list:
    datos = json.loads(ARCHIVO_PLATAFORMAS.read_text()).get(iss, [])
    ids = []
    for e in (datos if isinstance(datos, list) else [datos]):
        ids += e.get("deployment_ids", [])
    return ids


def _anotar_servicios(datos: dict, assignment_id=None):
    """Guarda dónde llamar a la lista y a las notas de este curso y esta actividad.

    Las direcciones solo viajan en el lanzamiento, y las llamadas se hacen mucho después
    —al importar, al firmar una corrección—, cuando ya no hay lanzamiento a mano.
    """
    nrps = (datos.get(CLAIM_NRPS) or {}).get("context_memberships_url", "")
    ags = datos.get(CLAIM_AGS) or {}
    contexto = (datos.get(CLAIM + "/context") or {}).get("id", "")
    enlace = (datos.get(CLAIM + "/resource_link") or {}).get("id", "")
    if not contexto:
        return
    client_id = (datos.get("aud") if isinstance(datos.get("aud"), str)
                 else (datos.get("aud") or [""])[0])
    lti_storage.guardar_servicios(
        datos.get("iss", ""), client_id, datos.get(f"{CLAIM}/deployment_id", ""),
        contexto, enlace, assignment_id, nrps, ags.get("lineitem", ""),
        " ".join(ags.get("scope") or []))


def _custom(datos: dict) -> dict:
    """Parámetros personalizados, descartando los que Moodle no supo sustituir.

    Si en el campus se configuró `lidia_dni=$User.username` y la sustitución falla,
    llega el literal «$User.username». Tomarlo como DNI sería peor que no tenerlo.
    """
    crudo = datos.get(f"{CLAIM}/custom") or {}
    limpio = {}
    for k, v in crudo.items():
        if isinstance(v, str) and v.startswith("$"):
            log.error("parámetro personalizado sin sustituir en el campus: %s=%s", k, v)
            continue
        limpio[k] = v
    return limpio


def _es_docente(datos: dict) -> bool:
    roles = datos.get(f"{CLAIM}/roles") or []
    return any(any(r.endswith(f"#{d}") or r.endswith(f"/{d}") for d in ROL_DOCENTE) for r in roles)


def _resolver_usuario(datos: dict, custom: dict):
    """Quién es en LidIA. Devuelve (fila de users, motivo del fallo)."""
    iss = datos.get("iss", "")
    client_id = (datos.get("aud") if isinstance(datos.get("aud"), str)
                 else (datos.get("aud") or [""])[0])
    despliegue = datos.get(f"{CLAIM}/deployment_id", "")
    sub = datos.get("sub", "")

    ya = lti_storage.identidad(iss, client_id, despliegue, sub)
    if ya:
        with get_db() as db:
            fila = db.execute("SELECT * FROM users WHERE id = ?", (ya["user_id"],)).fetchone()
        if fila:
            lti_storage.marcar_visto(ya["id"])
            return fila, ""
        # el usuario se borró de LidIA pero quedó la atadura: se limpia sola al revincular
        log.warning("identidad LTI %s apunta a un usuario inexistente", ya["id"])

    dni = str(custom.get("lidia_dni") or custom.get("lidia_idnumero") or "").strip()
    if not dni:
        return None, ("El campus no nos dijo tu documento. Es un problema de configuración "
                      "del campus, no tuyo: avisale al equipo docente.")
    with get_db() as db:
        fila = db.execute("SELECT * FROM users WHERE login = ?", (dni,)).fetchone()
    if not fila:
        if os.environ.get("LTI_AUTOALTA") == "1":
            return None, "AUTOALTA"   # reservado: todavía no implementado a propósito
        return None, (f"No encontramos a nadie con el documento {dni} en LidIA. "
                      "Puede que todavía no te hayan dado de alta: avisale al equipo docente.")

    lti_storage.vincular_identidad(
        iss, client_id, despliegue, sub, fila["id"], dni,
        datos.get("name", ""), datos.get("email", ""))
    log.info("identidad LTI vinculada: sub=%s → usuario %s (%s)", sub, fila["id"], dni)
    return fila, ""


@router.post("/launch")
async def launch(request: Request):
    """Moodle nos devuelve el token firmado: acá se entra a LidIA."""
    if not habilitado():
        return _error(request, "La integración no está configurada",
                      "Todavía no se conectó LidIA con el campus. Avisale al equipo docente.",
                      "falta la librería, plataformas.json o la clave privada", 503)
    lanzamiento, salida = await _validar(request)
    if lanzamiento is None:
        return salida
    datos = salida
    custom = _custom(datos)

    usuario, motivo = _resolver_usuario(datos, custom)
    if not usuario:
        return _error(request, "No pudimos identificarte", motivo,
                      f"sub={datos.get('sub')} custom={custom}", 403)
    if not usuario["active"] and usuario["role"] != "student":
        return _error(request, "Tu usuario está deshabilitado",
                      "Hablalo con la coordinación.", "", 403)

    aid = str(custom.get("lidia_instancia") or "").strip()
    _anotar_servicios(datos, int(aid) if aid.isdigit() else None)

    destino = _destino(request, datos, custom, usuario)
    if isinstance(destino, HTMLResponse):
        return destino

    from .main import BASE_PATH
    token = auth.create_session(usuario["id"])
    resp = RedirectResponse(destino, status_code=303)
    resp.set_cookie(auth.LTI_COOKIE_NAME, token, path=BASE_PATH or "/", **auth.cookie_lti())
    # El lanzamiento es una afirmación explícita de identidad: si en este navegador había
    # otra sesión de LidIA, manda la que acaba de llegar del campus. Sin esto, quien tenga
    # abierta su sesión docente entra al campus y sigue viéndose como docente.
    resp.delete_cookie(auth.COOKIE_NAME, path=BASE_PATH or "/")
    log.info("lanzamiento LTI ok: usuario %s → %s", usuario["id"], destino)
    return resp


def _destino(request: Request, datos: dict, custom: dict, usuario):
    """A qué pantalla de LidIA lleva este lanzamiento."""
    from .main import BASE_PATH
    aid = str(custom.get("lidia_instancia") or "").strip()
    if not aid.isdigit():
        # sin instancia elegida: al espacio de siempre
        return f"{BASE_PATH}/panel" if usuario["role"] == "student" else f"{BASE_PATH}/"

    with get_db() as db:
        assignment = get_assignment(db, int(aid))
        if not assignment:
            return _error(request, "La actividad apunta a algo que no existe",
                          "La instancia de evaluación fue borrada o cambió. Avisale al equipo docente.",
                          f"assignment_id={aid}", 404)
        edicion = get_edition(db, assignment["edition_id"])

        # La cursada de la instancia tiene que estar vinculada a este curso del campus:
        # si no, editar el parámetro a mano abriría la instancia de otra materia.
        contexto = (datos.get(f"{CLAIM}/context") or {}).get("id", "")
        iss = datos.get("iss", "")
        client_id = (datos.get("aud") if isinstance(datos.get("aud"), str)
                     else (datos.get("aud") or [""])[0])
        despliegue = datos.get(f"{CLAIM}/deployment_id", "")
        v = lti_storage.vinculo(iss, client_id, despliegue, contexto)
        if v and v["edition_id"] != edicion["id"]:
            return _error(request, "La actividad no corresponde a este curso",
                          "El equipo docente tiene que volver a elegir la instancia desde el campus.",
                          f"vínculo={v['edition_id']} instancia={edicion['id']}", 403)
        if not v and contexto:
            # Un curso sin vincular no tendría ninguna protección: cualquiera que edite el
            # parámetro de la actividad abriría la instancia de otra materia. Se ata en el
            # primer lanzamiento, y desde ahí se controla. Lo normal es que ya esté atado
            # por el selector; esto cubre las actividades creadas a mano.
            lti_storage.vincular_curso(iss, client_id, despliegue, contexto,
                                       edicion["id"], usuario["id"])
            log.info("vínculo establecido en el primer lanzamiento: curso %s → cursada %s",
                     contexto, edicion["id"])

        if usuario["role"] == "student":
            if not assignment["active"]:
                return _error(request, "Esta instancia todavía no está abierta",
                              "El equipo docente la activa cuando esté lista.", "", 403)
            if not is_enrolled(db, usuario["id"], edicion["id"]):
                return _error(request, "No estás inscripto en esta cursada",
                              f"Figurás en LidIA pero no en {edicion['nombre']}. "
                              "Avisale al equipo docente.", "", 403)
            return f"{BASE_PATH}/panel/instancia/{aid}"
    return f"{BASE_PATH}/admin/instancias/{aid}"


# ------------------------------------------------------------------ acto 3: el selector

@router.post("/deeplink")
async def deeplink(request: Request):
    """El docente agrega la actividad en el campus y elige qué instancia abre.

    Ese clic es el vínculo entre el curso del campus y la cursada de LidIA: no hay
    pantalla de configuración aparte ni nada que mantener sincronizado a mano.
    """
    if not habilitado():
        return _error(request, "La integración no está configurada",
                      "Todavía no se conectó LidIA con el campus. Avisale al equipo docente.",
                      "falta la librería, plataformas.json o la clave privada", 503)
    lanzamiento, salida = await _validar(request)
    if lanzamiento is None:
        return salida
    datos = salida
    if not _es_docente(datos):
        return _error(request, "Esto es para el equipo docente",
                      "Elegir la actividad la hace quien dicta la cursada.", "", 403)

    _anotar_servicios(datos)
    usuario, motivo = _resolver_usuario(datos, _custom(datos))
    if not usuario or usuario["role"] not in ("admin", "docente"):
        # Primer deep linking de este docente: todavía no sabemos quién es en LidIA.
        return _pedir_vinculo(request, datos, motivo)

    return await _mostrar_selector(request, _contexto_dl(datos, usuario["id"]), usuario)


def _contexto_dl(datos: dict, user_id: int) -> dict:
    return {
        "user_id": user_id,
        "iss": datos.get("iss"),
        "aud": datos.get("aud"),
        "sub": datos.get("sub"),
        "nombre": datos.get("name", ""),
        "deployment_id": datos.get(f"{CLAIM}/deployment_id"),
        "context_id": (datos.get(f"{CLAIM}/context") or {}).get("id", ""),
        "context_title": (datos.get(f"{CLAIM}/context") or {}).get("title", ""),
        "settings": datos.get("https://purl.imsglobal.org/spec/lti-dl/claim/deep_linking_settings"),
    }


async def _mostrar_selector(request: Request, guardado: dict, usuario):
    import secrets
    from .db import staff_editions
    from .main import BASE_PATH, templates
    with get_db() as db:
        cursadas = []
        for ed in staff_editions(db, usuario):
            insts = db.execute(
                "SELECT id, name, tipo, active FROM assignments WHERE edition_id = ? ORDER BY id",
                (ed["id"],)).fetchall()
            if insts:
                cursadas.append({"ed": ed, "instancias": insts})
    guardado = dict(guardado)
    guardado["user_id"] = usuario["id"]
    token = secrets.token_urlsafe(24)
    _almacen().set_value(f"dl-{token}", guardado, exp=lti_storage.TTL_SEGUNDOS)
    return templates.TemplateResponse(request, "lti_selector.html", {
        "cursadas": cursadas,
        "curso_campus": guardado.get("context_title") or "el curso",
        "token": token,
        "base": BASE_PATH,
    })


def _guardar_dl(datos: dict, user_id: int, lanzamiento=None) -> str:
    """Guarda el contexto del deep linking para poder responder cuando elija."""
    import secrets
    token = secrets.token_urlsafe(24)
    _almacen().set_value(f"dl-{token}", _contexto_dl(datos, user_id),
                         exp=lti_storage.TTL_SEGUNDOS)
    return token


@router.post("/deeplink/elegir")
async def deeplink_elegir(request: Request):
    """El docente eligió: se arma la respuesta firmada que Moodle espera."""
    if not habilitado():
        return _error(request, "La integración no está configurada",
                      "Todavía no se conectó LidIA con el campus. Avisale al equipo docente.",
                      "falta la librería, plataformas.json o la clave privada", 503)
    form = await request.form()
    token = (form.get("token") or "").strip()
    aid = (form.get("instancia") or "").strip()
    guardado = _almacen().get_value(f"dl-{token}")
    if not guardado or not aid.isdigit():
        return _error(request, "La selección venció",
                      "Volvé a agregar la actividad en el campus.", f"token={token!r}", 400)

    with get_db() as db:
        assignment = get_assignment(db, int(aid))
        if not assignment:
            return _error(request, "Esa instancia ya no existe", "Elegí otra.", "", 404)
        edicion = get_edition(db, assignment["edition_id"])

        # Ser docente en el campus no habilita nada acá: la asignación es de LidIA.
        # La coordinación sí puede vincular cualquier cursada, porque es quien asigna.
        quien = db.execute("SELECT * FROM users WHERE id = ?", (guardado["user_id"],)).fetchone()
        if not quien or not can_access_edition(db, quien, edicion["id"]):
            return _error(
                request, "No figurás como docente de esa cursada",
                f"En LidIA no estás asignado a {edicion['nombre']}, "
                "así que no podés vincularle una actividad. Pedile a la coordinación que "
                "te asigne y volvé a intentar.",
                f"usuario={guardado['user_id']} edicion={edicion['id']}", 403)

    client_id = (guardado["aud"] if isinstance(guardado["aud"], str)
                 else (guardado["aud"] or [""])[0])
    # Acá queda atado el curso del campus con la cursada de LidIA.
    lti_storage.vincular_curso(guardado["iss"], client_id, guardado["deployment_id"],
                               guardado["context_id"], edicion["id"], guardado["user_id"])

    from .main import templates
    from pylti1p3.deep_link import DeepLink
    from pylti1p3.deep_link_resource import DeepLinkResource
    from .lti_starlette import PedidoStarlette  # noqa: F401

    # Este pedido es nuestro formulario, no un lanzamiento: no hay registro que heredar.
    # Se arma la respuesta a mano con el registro de la plataforma que guardamos.
    cfg = _config()
    registro = cfg.find_registration_by_params(guardado["iss"], client_id)
    enlace = DeepLink(registro, guardado["deployment_id"], guardado["settings"] or {})

    recurso = (DeepLinkResource()
               .set_url(_url_publica("/lti/launch"))
               .set_title(f"{assignment['name']} — {edicion['nombre']}")
               .set_custom_params({"lidia_instancia": str(assignment["id"])}))

    jwt = enlace.get_response_jwt([recurso])
    log.info("deep linking: curso %s → cursada %s, instancia %s",
             guardado["context_id"], edicion["id"], assignment["id"])
    return templates.TemplateResponse(request, "lti_post.html", {
        "destino": (guardado["settings"] or {}).get("deep_link_return_url", ""),
        "jwt": jwt,
    })


def _url_publica(ruta: str) -> str:
    base = os.environ.get("LTI_URL_PUBLICA", "http://127.0.0.1:8080").rstrip("/")
    return f"{base}{ruta}"


def _pedir_vinculo(request: Request, datos: dict, motivo: str = "", err: str = ""):
    """Primer encuentro con un docente: que se identifique una vez y queda atado.

    Pasa siempre la primera vez, porque el usuario del campus y el de LidIA no tienen
    por qué llamarse igual: en el campus puede ser «jmfernandez» y acá el DNI. Después
    de esta vez la atadura manda y no se vuelve a pedir.
    """
    from .main import BASE_PATH, templates
    return templates.TemplateResponse(request, "lti_vincular.html", {
        "token": _guardar_dl(datos, 0, None),
        "nombre": datos.get("name", ""),
        "base": BASE_PATH,
        "err": err,
    }, status_code=200)


@router.post("/vincular")
async def vincular(request: Request):
    """Valida usuario y contraseña de LidIA y ata esa cuenta con la del campus."""
    if not habilitado():
        return _error(request, "La integración no está configurada",
                      "Todavía no se conectó LidIA con el campus. Avisale al equipo docente.",
                      "falta la librería, plataformas.json o la clave privada", 503)
    form = await request.form()
    token = (form.get("token") or "").strip()
    guardado = _almacen().get_value(f"dl-{token}")
    if not guardado:
        return _error(request, "La sesión venció",
                      "Volvé a agregar la actividad desde el campus.", "", 400)

    login = (form.get("login") or "").strip()
    clave = form.get("password") or ""
    ip = intentos.origen(request)
    if freno := intentos.bloqueado(login, ip):
        return _revincular(request, guardado, token, freno)
    with get_db() as db:
        fila = db.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    if not fila or not auth.verify_password(clave, fila["password_hash"]):
        intentos.fallo(login, ip)
        log.warning("vinculación fallida desde %s para el usuario %r", ip, login)
        return _revincular(request, guardado, token, "Usuario o contraseña incorrectos.")
    intentos.acierto(login, ip)
    if fila["role"] not in ("admin", "docente"):
        return _revincular(request, guardado, token,
                           "Esa cuenta no es de un docente: la actividad la configura el equipo docente.")

    client_id = (guardado["aud"] if isinstance(guardado["aud"], str)
                 else (guardado["aud"] or [""])[0])
    lti_storage.vincular_identidad(guardado["iss"], client_id, guardado["deployment_id"],
                                   guardado["sub"], fila["id"], "", guardado.get("nombre", ""), "")
    log.info("docente vinculado: sub=%s → usuario %s (%s)", guardado["sub"], fila["id"], login)
    return await _mostrar_selector(request, guardado, fila)


def _revincular(request: Request, guardado: dict, token: str, err: str):
    from .main import BASE_PATH, templates
    _almacen().set_value(f"dl-{token}", guardado, exp=lti_storage.TTL_SEGUNDOS)
    return templates.TemplateResponse(request, "lti_vincular.html", {
        "token": token, "nombre": guardado.get("nombre", ""), "base": BASE_PATH, "err": err,
    }, status_code=200)


# ------------------------------------------------------------------ traer del campus

def _cotejar(edicion_id: int, gente: list) -> dict:
    """Cruza la lista del campus con el padrón de LidIA.

    Devuelve tres grupos separados a propósito. «No está en el campus» y «no lo pude
    cruzar» se parecen en pantalla pero no son lo mismo: el segundo casi siempre es que
    el usuario del campus no es el DNI, y tiene arreglo mirando esa cuenta allá.
    """
    from .db import is_enrolled
    cruzados, sin_cruzar, faltan_alta = [], [], []
    dnis_campus = set()

    with get_db() as db:
        for m in gente:
            if not m["estudiante"]:
                continue
            dni = m["usuario"] or m["sourcedid"]
            fila = None
            if dni:
                dnis_campus.add(dni)
                fila = db.execute("SELECT * FROM users WHERE login = ?", (dni,)).fetchone()
            if fila:
                cruzados.append({
                    "m": m, "usuario": fila,
                    "inscripto": is_enrolled(db, fila["id"], edicion_id),
                    "sin_correo": not (fila["email"] or "").strip(),
                })
            elif dni:
                faltan_alta.append(m)      # está en el campus, no en LidIA: se puede crear
            else:
                sin_cruzar.append(m)       # el campus no mandó con qué cruzarlo

        # Y al revés: inscriptos en LidIA que la lista del campus no trajo.
        sobrantes = []
        for u in db.execute(
            "SELECT u.* FROM enrollments e JOIN users u ON u.id = e.user_id"
            " WHERE e.edition_id = ? AND u.role = 'student' ORDER BY u.apellido, u.nombre",
            (edicion_id,),
        ).fetchall():
            if u["login"] not in dnis_campus:
                n = db.execute(
                    "SELECT COUNT(*) n FROM submissions s JOIN assignments a ON a.id = s.assignment_id"
                    " WHERE s.user_id = ? AND a.edition_id = ?", (u["id"], edicion_id)).fetchone()["n"]
                sobrantes.append({"usuario": u, "entregas": n})

    return {"cruzados": cruzados, "faltan_alta": faltan_alta,
            "sin_cruzar": sin_cruzar, "sobrantes": sobrantes}


@router.get("/importar/{cid}", response_class=HTMLResponse)
def importar(request: Request, cid: int):
    """Muestra la lista del campus cotejada con el padrón de LidIA."""
    from .main import _require, STAFF, templates
    user, salida = _require(request, *STAFF)
    if salida:
        return salida

    from . import lti_servicios
    with get_db() as db:
        edicion = get_edition(db, cid)
        if not edicion or not can_access_edition(db, user, cid):
            from .main import redirect
            return redirect("/admin/cursos")
    servicio = lti_storage.servicios_de_cursada(cid)
    if not servicio:
        from .main import redirect
        return redirect(f"/admin/cursos/{cid}", err=(
            "Esta cursada todavía no está vinculada a un curso del campus. Agregá la "
            "Herramienta externa en Moodle y elegí una de sus instancias: con eso queda "
            "vinculada y se puede traer la lista."))
    try:
        gente = lti_servicios.lista(servicio["iss"], servicio["nrps_url"])
    except lti_servicios.ErrorServicio as exc:
        from .main import redirect
        return redirect(f"/admin/cursos/{cid}", err=str(exc))

    return templates.TemplateResponse(request, "lti_importar.html", {
        "course": edicion, **_cotejar(cid, gente), "total_campus": len(gente),
    })


@router.post("/importar/{cid}")
async def importar_confirmar(request: Request, cid: int):
    """Crea en LidIA a quienes falten e inscribe a todos los que se hayan marcado."""
    from .main import _require, STAFF, redirect
    user, salida = _require(request, *STAFF)
    if salida:
        return salida
    form = await request.form()
    elegidos = set(form.getlist("dni"))

    from .db import enroll
    from . import lti_servicios
    with get_db() as db:
        edicion = get_edition(db, cid)
        if not edicion or not can_access_edition(db, user, cid):
            return redirect("/admin/cursos")
    servicio = lti_storage.servicios_de_cursada(cid)
    if not servicio:
        return redirect(f"/admin/cursos/{cid}", err="La cursada no está vinculada al campus.")
    try:
        gente = lti_servicios.lista(servicio["iss"], servicio["nrps_url"])
    except lti_servicios.ErrorServicio as exc:
        return redirect(f"/admin/cursos/{cid}", err=str(exc))

    creados = inscriptos = 0
    with get_db() as db:
        for m in gente:
            dni = m["usuario"] or m["sourcedid"]
            if not m["estudiante"] or not dni or dni not in elegidos:
                continue
            fila = db.execute("SELECT * FROM users WHERE login = ?", (dni,)).fetchone()
            if not fila:
                # Sin contraseña a propósito: entran por el campus y no la necesitan.
                # Si alguna vez quieren entrar directo, la piden por correo.
                # Sin contraseña utilizable: quien llega del campus entra por el
                # lanzamiento, y si alguna vez quiere entrar derecho a LidIA pide su enlace.
                # Un hash vacío es peligroso: según con qué se lo compare, podría validar.
                apellido, nombre = _nombre_del_campus(m)
                uid = db.execute(
                    "INSERT INTO users (login, password_hash, apellido, nombre, full_name,"
                    " email, role, active, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'student', 1, ?)",
                    (dni, claves.clave_inutilizable(), apellido, nombre,
                     nombre_completo(apellido, nombre), m["email"], utcnow()),
                ).lastrowid
                creados += 1
            else:
                uid = fila["id"]
                if m["email"] and not (fila["email"] or "").strip():
                    db.execute("UPDATE users SET email = ? WHERE id = ?", (m["email"], uid))
            if enroll(db, uid, cid):
                inscriptos += 1

    log.info("importación desde el campus: cursada %s, %s creados, %s inscriptos",
             cid, creados, inscriptos)
    partes = []
    if creados:
        partes.append(f"{creados} estudiante{'s' if creados != 1 else ''} dado"
                      f"{'s' if creados != 1 else ''} de alta")
    if inscriptos:
        partes.append(f"{inscriptos} inscripto{'s' if inscriptos != 1 else ''}")
    return redirect(f"/admin/cursos/{cid}",
                    msg=("Listo: " + " y ".join(partes) + ".") if partes else
                        "No había nada nuevo para traer.")


def _nombre_del_campus(m: dict) -> tuple[str, str]:
    """(apellido, nombre) de un miembro del campus.

    Se usan los campos separados que manda el campus, que son los correctos. Solo si no
    vinieran se cae al nombre completo, y ahí NO se adivina el corte: va entero al
    apellido. Antes se tomaba la última palabra como apellido, y con dos apellidos —lo más
    común acá— salía siempre mal: «Ana Suárez Pérez» quedaba como «Pérez, Ana Suárez».
    """
    apellido = (m.get("apellido") or "").strip()
    nombre = (m.get("nombre_pila") or "").strip()
    if apellido or nombre:
        return apellido, nombre
    completo = (m.get("nombre") or "").strip()
    return (completo or "(sin nombre)"), ""


# ------------------------------------------------------------------ la nota de vuelta

def enviar_nota_al_campus(assignment_id: int, user_id: int, nota) -> str:
    """Manda al libro del campus la nota que se acaba de firmar.

    Devuelve un texto para agregar al aviso de pantalla, o cadena vacía si no aplica.
    Nunca lanza: que falle el campus no puede impedir que se firme una corrección.
    """
    if nota is None or not habilitado():
        return ""
    servicio = lti_storage.servicios_de_instancia(assignment_id)
    if not servicio:
        return ""     # esta instancia no llegó desde ningún campus

    from . import lti_servicios
    try:
        with get_db() as db:
            ident = db.execute(
                "SELECT sub FROM lti_identidades WHERE user_id = ? AND iss = ?"
                " AND client_id = ? AND deployment_id = ?",
                (user_id, servicio["iss"], servicio["client_id"], servicio["deployment_id"]),
            ).fetchone()
        if not ident:
            return " (al campus no se mandó: esta persona nunca entró desde ahí)"

        lti_servicios.enviar_nota(
            servicio["iss"], servicio["lineitem_url"], ident["sub"], float(nota), 10,
            "Corregido en LidIA.")

        # No alcanza con que el campus haya contestado 200: si la actividad allá no tiene
        # calificación numérica, la acepta y la descarta. Se comprueba leyéndola de vuelta.
        quedo, _ = lti_servicios.quedo_registrada(
            servicio["iss"], servicio["lineitem_url"], ident["sub"])
        if quedo is False:
            return (" ⚠ El campus aceptó la nota pero no la registró: esa actividad está"
                    " configurada sin calificación numérica. Corregilo en Moodle, en los"
                    " ajustes de la actividad, y volvé a firmar.")
        if quedo is None:
            return f" Se mandó al campus ({nota:g} de 10), pero no se pudo confirmar allá."
        return f" Y quedó registrada en el campus ({nota:g} de 10)."
    except lti_servicios.ErrorServicio as exc:
        log.warning("no se pudo enviar la nota al campus: %s", exc)
        return f" (no se pudo registrar en el campus: {exc})"
    except Exception as exc:  # noqa: BLE001
        log.exception("error inesperado enviando la nota al campus")
        return " (no se pudo registrar en el campus)"


# ------------------------------------------------------------------ cotejo de notas

def _comparar_notas(assignment_id: int):
    """Compara las notas firmadas en LidIA con las que hoy tiene el campus.

    Devuelve (servicio, filas, error). Cada fila dice de quién es, qué dice cada lado
    y en qué estado está: iguales, distintas, sin mandar, o solo en el campus.
    """
    from . import lti_servicios
    servicio = lti_storage.servicios_de_instancia(assignment_id)
    if not servicio:
        return None, [], "Esta instancia no está vinculada a ninguna actividad del campus."
    try:
        del_campus = lti_servicios.resultados(servicio["iss"], servicio["lineitem_url"])
    except lti_servicios.ErrorServicio as exc:
        return servicio, [], str(exc)

    filas = []
    vistos = set()
    with get_db() as db:
        firmadas = db.execute(
            "SELECT s.user_id, s.nota, s.reviewed_at, u.full_name, u.login, i.sub"
            "  FROM submissions s"
            "  JOIN users u ON u.id = s.user_id"
            "  LEFT JOIN lti_identidades i ON i.user_id = s.user_id AND i.iss = ?"
            "       AND i.client_id = ? AND i.deployment_id = ?"
            " WHERE s.assignment_id = ? AND s.kind = 'final' AND s.status = 'aprobada'"
            "   AND s.nota IS NOT NULL ORDER BY u.apellido, u.nombre",
            (servicio["iss"], servicio["client_id"], servicio["deployment_id"], assignment_id),
        ).fetchall()

        for f in firmadas:
            sub = f["sub"]
            alla = del_campus.get(str(sub)) if sub else None
            if sub:
                vistos.add(str(sub))
            if not sub:
                estado = "sin_identidad"
            elif alla is None:
                estado = "sin_mandar"
            elif abs(float(alla["nota"]) - float(f["nota"])) < 0.01:
                estado = "igual"
            else:
                estado = "distinta"
            filas.append({
                "user_id": f["user_id"], "nombre": f["full_name"], "login": f["login"],
                "sub": sub, "aca": f["nota"], "alla": alla["nota"] if alla else None,
                "estado": estado,
            })

        # Notas que existen en el campus y no salieron de acá: alguien calificó allá.
        for sub, datos in del_campus.items():
            if sub in vistos:
                continue
            ident = db.execute(
                "SELECT u.id, u.full_name, u.login FROM lti_identidades i"
                "  JOIN users u ON u.id = i.user_id"
                " WHERE i.sub = ? AND i.iss = ? AND i.client_id = ? AND i.deployment_id = ?",
                (sub, servicio["iss"], servicio["client_id"], servicio["deployment_id"]),
            ).fetchone()
            filas.append({
                "user_id": ident["id"] if ident else None,
                "nombre": ident["full_name"] if ident else f"(usuario {sub} del campus)",
                "login": ident["login"] if ident else "",
                "sub": sub, "aca": None, "alla": datos["nota"], "estado": "solo_alla",
            })
    return servicio, filas, ""


@router.get("/notas/{aid}", response_class=HTMLResponse)
def notas(request: Request, aid: int):
    """Qué dice LidIA y qué dice el campus sobre las notas de esta instancia."""
    from .main import _require, STAFF, redirect, templates
    user, salida = _require(request, *STAFF)
    if salida:
        return salida
    with get_db() as db:
        assignment = get_assignment(db, aid)
        if not assignment or not can_access_edition(db, user, assignment["edition_id"]):
            return redirect("/admin/instancias")
        edicion = get_edition(db, assignment["edition_id"])

    servicio, filas, err = _comparar_notas(aid)
    if err and not servicio:
        return redirect(f"/admin/instancias/{aid}", err=err)
    return templates.TemplateResponse(request, "lti_notas.html", {
        "assignment": assignment, "course": edicion, "filas": filas, "err": err,
    })


@router.post("/notas/{aid}")
async def notas_sincronizar(request: Request, aid: int):
    """Manda al campus las notas de LidIA que se hayan marcado."""
    from . import lti_servicios
    from .main import _require, STAFF, redirect
    user, salida = _require(request, *STAFF)
    if salida:
        return salida
    form = await request.form()
    elegidos = set(form.getlist("user_id"))

    with get_db() as db:
        assignment = get_assignment(db, aid)
        if not assignment or not can_access_edition(db, user, assignment["edition_id"]):
            return redirect("/admin/instancias")

    servicio, filas, err = _comparar_notas(aid)
    if err:
        return redirect(f"/lti/notas/{aid}", err=err)

    enviadas, fallidas, mandadas = 0, [], []
    for f in filas:
        # Solo las que el campus no tiene. Las divergentes se avisan y se arreglan allá:
        # una nota anulada a mano en el libro ignora para siempre lo que mande la actividad.
        if f["estado"] != "sin_mandar":
            continue
        if str(f["user_id"]) not in elegidos or f["aca"] is None or not f["sub"]:
            continue
        try:
            lti_servicios.enviar_nota(servicio["iss"], servicio["lineitem_url"], f["sub"],
                                      float(f["aca"]), 10, "Corregido en LidIA.")
            enviadas += 1
            mandadas.append(f)
        except lti_servicios.ErrorServicio as exc:
            fallidas.append(f"{f['nombre']}: {exc}")

    # Comprobar que hayan quedado, no suponerlo. Si en el libro de Moodle alguien editó
    # esa nota a mano, Moodle la marca como anulada e ignora para siempre lo que mande
    # la actividad: la nuestra entra igual pero el estudiante sigue viendo la de allá.
    anuladas = []
    if mandadas:
        try:
            ahora = lti_servicios.resultados(servicio["iss"], servicio["lineitem_url"])
            for f in mandadas:
                alla = ahora.get(str(f["sub"]))
                if alla and abs(float(alla["nota"]) - float(f["aca"])) >= 0.01:
                    anuladas.append(f["nombre"])
        except lti_servicios.ErrorServicio:
            pass

    aviso = f"{enviadas} nota{'s' if enviadas != 1 else ''} enviada{'s' if enviadas != 1 else ''} al campus."
    if anuladas:
        return redirect(f"/lti/notas/{aid}", err=(
            aviso + f" Pero {len(anuladas)} sigue{'n' if len(anuladas) != 1 else ''} sin cambiar en el "
            "campus: esas notas están anuladas manualmente en el libro de Moodle, y una nota anulada "
            "ignora lo que mande la actividad. Hay que quitar la anulación desde el libro de "
            f"calificaciones. Son: {', '.join(anuladas[:5])}."))
    if fallidas:
        return redirect(f"/lti/notas/{aid}", err=aviso + " No se pudieron mandar: " + "; ".join(fallidas[:3]))
    return redirect(f"/lti/notas/{aid}", msg=aviso)
