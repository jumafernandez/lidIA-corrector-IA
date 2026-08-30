"""LidIA — devoluciones formativas con IA. LICDIA · UNLu."""
import csv
import difflib
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import markdown as md_lib
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, PlainTextResponse, HTMLResponse, JSONResponse,
                               RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (archivos, auth, choice as choice_mod, circuito as circ, claves,
               examen as examen_mod, intentos,
               investigacion, lti, lti_storage, modelos, repos)
from .db import (ahora_local, all_courses, anio_actual, momento_apertura, momento_cierre,
                 periodo_sql, ventana_entrega, fecha_corta, assignment_cfg,
                 assignment_items, can_access_edition,
                 inscripcion_habilitada,
                 grupo_de, miembros_de,
                 course_editions, edition_assignments, edition_teachers, enroll, final_activa,
                 get_assignment, get_config, get_course, get_db, get_edition, init_db,
                 is_enrolled, items_puntaje_total, practicas_usadas, preguntas_usadas,
                 set_config, staff_editions, student_editions, teacher_edition_ids,
                 utcnow, visible_courses)
from . import emailer
from .emailer import desvio, smtp_configured
from .extract import ExtractionError, contar_imagenes, extract_text, paginas_de_pdf
from .llm import (LLMError, answer_question, criterios_de, explicar_errores,
                  generate_feedback, leer_respuestas_faltantes, model_info,
                  nota_de_niveles, nota_de_puntajes, puntuar_examen,
                  revisar_integridad, separar_niveles, split_items, transcribe_images)

BASE_DIR = os.path.dirname(__file__)
# prefijo bajo el que se sirve la app detrás del proxy (ej.: /entregas). Vacío en local.
BASE_PATH = "/" + os.environ.get("BASE_PATH", "").strip("/") if os.environ.get("BASE_PATH", "").strip("/") else ""
app = FastAPI(title="LidIA", docs_url=None, redoc_url=None, root_path=BASE_PATH)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["base"] = BASE_PATH
templates.env.globals["desvio_correo"] = desvio

AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
STAFF = ("admin", "docente")
LOGIN_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")


def md(text: str) -> str:
    """Markdown → HTML, con el HTML crudo neutralizado."""
    safe = (text or "").replace("<", "&lt;")
    return md_lib.markdown(safe, extensions=["extra", "sane_lists"])


def fecha(value: str) -> str:
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(AR_TZ).strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError):
        return value or ""


def first_name(full_name: str) -> str:
    full_name = (full_name or "").strip()
    if "," in full_name:  # "Gómez, María"
        return full_name.split(",", 1)[1].strip().split(" ")[0]
    return full_name.split(" ")[0] if full_name else ""


templates.env.filters["md"] = md
templates.env.filters["fecha"] = fecha
templates.env.filters["nombre_pila"] = first_name
templates.env.filters["fecha_corta"] = fecha_corta
log = logging.getLogger("lidia")
app.include_router(lti.router)


@app.middleware("http")
async def origen_confiable(request: Request, call_next):
    """Rechaza los POST que vienen de otro sitio.

    LidIA se defendía de CSRF con `SameSite=Lax` en su cookie de sesión. La cookie que
    usan quienes entran desde el campus tiene que ser `SameSite=None` —si no, el
    lanzamiento entre sitios no funciona—, y eso deja esas sesiones sin esa protección
    en TODAS las rutas, no solo en las de LTI. Esto repone lo que se cedió.

    Solo mira el encabezado `Origin`, que el navegador pone solo y no se puede falsificar
    desde una página. Si no viene (peticiones del mismo sitio en algunos navegadores,
    curl, un formulario viejo), se deja pasar: el objetivo es cortar el envío desde otro
    sitio, no romper lo que ya andaba.

    Las rutas de /lti/ quedan afuera a propósito: ahí el POST entre sitios es el
    mecanismo, y su protección es la firma del token, que es más fuerte que esto.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        ruta = request.url.path
        origen = request.headers.get("origin")
        if origen and not ruta.startswith(f"{BASE_PATH}/lti/" if BASE_PATH else "/lti/"):
            propio = f"{request.url.scheme}://{request.url.netloc}"
            permitidos = {propio}
            extra = os.environ.get("ORIGENES_PERMITIDOS", "").strip()
            if extra:
                permitidos |= {o.strip().rstrip("/") for o in extra.split(",") if o.strip()}
            if origen.rstrip("/") not in permitidos:
                log.warning("POST rechazado por origen ajeno: %s → %s", origen, ruta)
                return PlainTextResponse(
                    "Ese envío no viene de LidIA. Si llegaste acá desde otro sitio, "
                    "volvé a entrar desde la aplicación.", status_code=403)
    return await call_next(request)


@app.on_event("startup")
def _startup():
    init_db()
    lti_storage.init_lti_db()
    claves.init_claves_db()
    huerfanos = archivos.limpiar_huerfanos()
    if huerfanos:
        log.info("Se borraron %s carpetas de entregas sin dueño.", huerfanos)
    intentos.init_intentos_db()


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", auth.current_user(request))
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx.setdefault("err", request.query_params.get("err", ""))
    ctx.setdefault("correo", request.query_params.get("correo", ""))
    return templates.TemplateResponse(request, template, ctx)


def redirect(url: str, msg: str = "", err: str = "", correo: str = "") -> RedirectResponse:
    # `correo` es un canal aparte y no una frase pegada al final del mensaje: que salga o
    # no un correo es una consecuencia distinta de la acción que se hizo, y mezclarlas hace
    # que nadie la lea. Además, en pruebas, un correo que no salió tiene que notarse.
    for clave, valor in (("msg", msg), ("err", err), ("correo", correo)):
        if valor:
            url += ("&" if "?" in url else "?") + clave + "=" + quote(valor)
    return RedirectResponse(BASE_PATH + url, status_code=303)


def aviso_correo(ok: bool, detalle: str) -> str:
    """El aviso que ve quien disparó el correo. La frase ya la arma `emailer`."""
    return detalle


def _require(request: Request, *roles: str):
    user = auth.current_user(request)
    if not user:
        return None, redirect("/login")
    if user["role"] not in roles:
        return None, redirect("/")
    return user, None


def puede_crear_materias(user) -> bool:
    """¿Este usuario puede dar de alta materias y cursadas?

    La coordinación siempre. Los docentes, solo si la coordinación lo habilitó: es la
    diferencia entre que cada docente arme su cursada cuando la necesita y que tenga que
    pedirla todos los cuatrimestres. El riesgo de abrirlo es que aparezcan tres veces la
    misma materia con nombres distintos; por eso es una decisión y no un valor fijo.
    """
    if not user:
        return False
    if user["role"] == "admin":
        return True
    if user["role"] != "docente":
        return False
    with get_db() as db:
        return get_config(db).get("docentes_crean_materias", "1") == "1"


def puede_ver_materia(db, user, mid: int) -> bool:
    """¿Le corresponde ver la ficha de esta materia? Mismo criterio que el listado."""
    if user["role"] == "admin":
        return True
    return any(m["id"] == mid for m in visible_courses(db, user))


def puede_editar_materia(db, user, mid: int) -> bool:
    """¿Puede cambiarle el nombre y la vigencia a esta materia?

    Quien puede crear puede corregir lo que creó. El único límite es que la materia sea
    suya: renombrarla la cambia en todas sus cursadas, así que un docente la edita solo
    si todas las cursadas de esa materia son suyas —incluida la recién creada, que no
    tiene ninguna—. La coordinación edita cualquiera.
    """
    if not puede_crear_materias(user):
        return False
    ids = teacher_edition_ids(db, user)
    if ids is None:
        return True
    eds = course_editions(db, mid)
    if not eds:
        # Sin cursadas no hay «todas suyas» que valga: vale haberla creado.
        return get_course(db, mid)["creado_por"] == user["id"]
    return all(ed["id"] in ids for ed in eds)

# Se registra acá y no arriba porque la función se define en este punto del módulo.
templates.env.globals["puede_crear"] = puede_crear_materias


def _corregir_choice(cfg, nombre, work_text, marcadas=None):
    """Corrige un multiple choice: la nota por código, la explicación por el modelo.

    Comparar letras y sumar puntajes es aritmética, y una aritmética equivocada por un
    modelo no se nota: la devolución igual suena bien. Acá el resultado sale de código, y
    el modelo se ocupa de lo único que una cuenta no puede hacer, que es explicarle a
    alguien por qué se equivocó.

    Devuelve (markdown, modelo, telemetría, nota).
    """
    items = cfg.get("items") or []
    if not items:
        raise LLMError("La instancia no tiene preguntas cargadas: no hay contra qué corregir.")

    if marcadas:
        # Camino normal: se respondió marcando, así que no hay nada que interpretar.
        resultado = choice_mod.corregir_marcadas(items, marcadas)
    else:
        # Camino del examen en papel: lo único que hay es la transcripción de la hoja.
        resultado = choice_mod.corregir(items, work_text)
        faltantes = [d["n"] for d in resultado["detalle"] if d["sin_responder"]]
        if faltantes:
            extra = leer_respuestas_faltantes(work_text, faltantes)
            if extra:
                texto = work_text + "\n" + "\n".join(f"{n}-{l}" for n, l in extra.items())
                resultado = choice_mod.corregir(items, texto)

    errores = [d for d in resultado["detalle"] if not d["acerto"]]
    explicacion, modelo, tele = "", "", {}
    if errores:
        try:
            explicacion, modelo, tele = explicar_errores(cfg, nombre, errores)
        except LLMError as exc:
            # La nota ya está: comparar letras y sumar puntajes es aritmética y no necesitó
            # al modelo. Perderla porque falló la parte que solo explica sería tirar lo que
            # sí salió bien. Queda la tabla, que muestra pregunta por pregunta qué pasó.
            log.warning("no se pudo explicar los errores del choice: %s", exc)
            explicacion = ("_No se pudo generar la explicación de los errores en este "
                           "momento. El detalle de cada pregunta está en la tabla._")

    partes = ["## Resultado", choice_mod.tabla_markdown(resultado)]
    if explicacion:
        partes += ["## Por qué", explicacion]
    elif not errores:
        partes.append("Respondiste todo bien. No hay nada que corregir acá.")
    return "\n\n".join(partes), modelo or "deterministico", tele, resultado["nota"]


def _incidentes(db, sub) -> dict:
    """Lo que quedó registrado mientras rendía, listo para la ficha.

    Lo ven las dos partes: quien corrige, para poder mirarlo antes de firmar; y quien
    rindió, porque se le avisó en el momento y sería raro escondérselo después.
    """
    filas = examen_mod.de_entrega(db, sub["id"])
    datos = examen_mod.resumen(filas)
    datos["filas"] = filas
    return datos


def _detalle_nota(sub) -> dict:
    """El desglose guardado de la calificación, listo para mostrar."""
    crudo = sub["detalle_nota"] if "detalle_nota" in sub.keys() else ""
    if not (crudo or "").strip():
        return {}
    try:
        return json.loads(crudo)
    except (ValueError, TypeError):
        return {}


def _niveles(sub) -> list:
    """Los niveles por criterio guardados con la entrega, listos para mostrar."""
    crudo = sub["niveles"] if "niveles" in sub.keys() else ""
    if not (crudo or "").strip():
        return []
    try:
        return json.loads(crudo)
    except (ValueError, TypeError):
        return []


def _repo_leido(sub) -> str:
    """Qué se leyó del repositorio al corregir, para que el docente sepa contra qué se corrigió.

    Se guarda al momento de la entrega y no se vuelve a consultar: el repositorio cambia
    después, y la devolución tiene que poder explicarse con lo que había entonces.
    """
    crudo = sub["repo_resumen"] if "repo_resumen" in sub.keys() else ""
    if not (crudo or "").strip():
        return ""
    try:
        return repos.resumen_legible(json.loads(crudo))
    except (ValueError, TypeError):
        return ""


def _mandar_enlace(fila, volver: str, motivo: str = "olvido"):
    """Le manda a esta persona el enlace para elegir contraseña.

    Reemplaza al viejo «restablecer»: la coordinación ya no genera ni conoce contraseñas,
    solo dispara el correo. Si la cuenta no tiene correo cargado no hay a dónde mandarlo,
    y eso hay que decirlo en vez de fingir que se mandó.
    """
    correo = (fila["email"] or "").strip()
    if not correo:
        return redirect(volver, err=(
            f"{fila['full_name']} no tiene correo cargado, así que no hay a dónde mandarle "
            "el enlace. Cargáselo y volvé a intentar."))
    token = claves.crear(fila["id"], motivo)
    if not token:
        return redirect(volver, err="Ya se mandaron varios enlaces a esta cuenta hace poco. "
                                    "Esperá un rato antes de pedir otro.")
    enlace = emailer.url_absoluta(f"/clave/{token}")
    armar = emailer.invitacion if motivo == "alta" else emailer.recuperacion
    ok, detalle = emailer.enviar(correo, armar(first_name(fila["full_name"]), fila["login"], enlace))
    if not ok:
        return redirect(volver, correo=aviso_correo(False, detalle))
    vence = "una semana" if motivo == "alta" else "dos horas"
    return redirect(volver, msg=f"Enlace de contraseña generado para {fila['full_name']}.",
                    correo=aviso_correo(True, f"{detalle} El enlace vence en {vence}."))


def _consejo(db, user, pantalla: str):
    """Lo que Lidia tiene para decir en esta pantalla, o None si no falta nada.

    Se calcula acá y no en la plantilla porque necesita la base. Cuando la persona no
    tiene ninguna cursada no hay circuito que mirar, así que se responde por el otro lado.
    """
    cursadas = staff_editions(db, user)
    if not cursadas:
        return circ.consejo_sin_cursadas(puede_crear_materias(user))
    return circ.consejo(db, cursadas, pantalla)


def _scope_ids(db, user):
    """Cursos administrables: None = todos (admin), lista (posiblemente vacía) para docentes."""
    return teacher_edition_ids(db, user)


def _course_cond(col: str, ids: list) -> tuple[str, list]:
    """Condición 'col IN (...)'. Lista vacía → condición imposible."""
    if not ids:
        return "1 = 0", []
    return f"{col} IN ({','.join('?' * len(ids))})", list(ids)


def _curso_param(curso: str | None) -> int | None:
    """El filtro de curso llega como query string; vacío = sin filtro."""
    try:
        return int(curso) if curso else None
    except ValueError:
        return None


def _student_in_scope(db, user, student_id: int) -> bool:
    ids = _scope_ids(db, user)
    if ids is None:
        return True
    if not ids:
        return False
    marks = ",".join("?" * len(ids))
    return bool(db.execute(
        f"SELECT 1 FROM enrollments WHERE user_id = ? AND edition_id IN ({marks})",
        [student_id, *ids],
    ).fetchone())


def _load_submission(db, sid: int):
    """Entrega + su instancia y curso. Devuelve (sub, assignment, course) o (None, None, None)."""
    sub = db.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    if not sub:
        return None, None, None
    assignment = get_assignment(db, sub["assignment_id"])
    course = get_edition(db, assignment["edition_id"])
    return sub, assignment, course


# ---------------------------------------------------------------- sesión

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Inicio: qué hay para hacer hoy, no un listado más.

    Para el equipo docente lo primero es la cola de correcciones firmadas por nadie —que
    es el trabajo real— y después lo que está por vencer. Al estudiantado le sirve su
    espacio de siempre, que ya está armado alrededor de sus cursos.
    """
    user = auth.current_user(request)
    if not user:
        return redirect("/login")
    if user["role"] not in STAFF:
        return redirect("/panel")

    hoy = datetime.now(AR_TZ).date().isoformat()
    es_coord = user["role"] == "admin"
    with get_db() as db:
        # _scope_ids devuelve None para coordinación (ve todo) y una lista para docentes.
        ids = _scope_ids(db, user)
        if ids is None:
            filtro, args = "1 = 1", []
        else:
            filtro, args = _course_cond("a.edition_id", ids)

        pendientes = db.execute(
            f"""SELECT s.id, s.created_at, u.full_name AS alumno, a.name AS instancia,
                       c.name AS materia, {periodo_sql('ed')} AS periodo, s.error
                  FROM submissions s
                  JOIN users u ON u.id = s.user_id
                  JOIN assignments a ON a.id = s.assignment_id
                  JOIN course_editions ed ON ed.id = a.edition_id
                  JOIN courses c ON c.id = ed.course_id
                 WHERE {filtro} AND s.kind = 'final' AND s.status = 'pendiente'
                 ORDER BY s.created_at""", args).fetchall()

        porvencer = db.execute(
            f"""SELECT a.id, a.name, a.fecha_cierre, c.name AS materia, {periodo_sql('ed')} AS periodo,
                       (SELECT COUNT(*) FROM enrollments e WHERE e.edition_id = ed.id) AS inscriptos,
                       (SELECT COUNT(DISTINCT s.user_id) FROM submissions s
                         WHERE s.assignment_id = a.id AND s.kind = 'final') AS entregaron
                  FROM assignments a
                  JOIN course_editions ed ON ed.id = a.edition_id
                  JOIN courses c ON c.id = ed.course_id
                 WHERE {filtro} AND a.active = 1 AND a.requiere_revision = 1
                       AND COALESCE(a.fecha_cierre, '') != '' AND a.fecha_cierre >= ?
                 ORDER BY a.fecha_cierre LIMIT 5""", (*args, hoy)).fetchall()

        cursadas = []
        for c in staff_editions(db, user):
            cursadas.append({
                "c": c,
                "n_est": db.execute("SELECT COUNT(*) n FROM enrollments WHERE edition_id = ?",
                                    (c["id"],)).fetchone()["n"],
                "n_inst": db.execute("SELECT COUNT(*) n FROM assignments WHERE edition_id = ?",
                                     (c["id"],)).fetchone()["n"],
                "pendientes": sum(1 for p in pendientes
                                  if p["materia"] == c["materia"] and p["periodo"] == c["periodo"]),
            })

        # Cosas que degradan la corrección sin que nadie se entere. Solo las ve quien puede
        # resolverlas: la coordinación en todo el laboratorio, el docente en lo suyo.
        avisos = []
        sin_programa = [x["c"] for x in cursadas
                        if x["c"]["active"] and not (x["c"]["programa"] or "").strip()]
        if sin_programa:
            avisos.append({
                "texto": f"{len(sin_programa)} cursada{'s' if len(sin_programa) != 1 else ''} abierta"
                         f"{'s' if len(sin_programa) != 1 else ''} sin programa cargado: Lidia corrige"
                         " sin saber qué se enseñó.",
                "url": f"/admin/cursos/{sin_programa[0]['id']}/programa",
                "accion": "Cargar el primero",
            })
        borradores = db.execute(
            f"SELECT COUNT(*) n FROM assignments a WHERE {filtro} AND a.active = 0", args
        ).fetchone()["n"]
        if borradores:
            avisos.append({
                "texto": f"{borradores} instancia{'s' if borradores != 1 else ''} en borrador:"
                         " el estudiantado todavía no la"
                         f"{'s' if borradores != 1 else ''} ve.",
                "url": "/admin/instancias", "accion": "Ver instancias",
            })
        if es_coord:
            sin_correo = db.execute(
                "SELECT COUNT(*) n FROM users WHERE role = 'student' AND active = 1"
                " AND COALESCE(email, '') = ''").fetchone()["n"]
            if sin_correo:
                avisos.append({
                    "texto": f"{sin_correo} estudiante{'s' if sin_correo != 1 else ''} sin correo"
                             " cargado: no recibe la devolución final por mail.",
                    "url": "/admin/estudiantes", "accion": "Ver estudiantes",
                })

    return render(request, "home.html", pendientes=pendientes, cursadas=cursadas,
                  porvencer=porvencer, avisos=avisos, es_coord=es_coord)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if auth.current_user(request):
        return redirect("/")
    return render(request, "login.html")


@app.post("/login")
def login(request: Request, login_id: str = Form(...), password: str = Form(...)):
    login_id = login_id.strip()
    ip = intentos.origen(request)
    # Tope duro primero, solo para no gastar CPU en el hash si alguien está martillando.
    if intentos.abuso(login_id):
        return redirect("/login", err="Demasiados intentos. Esperá unos minutos.")
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE login = ?", (login_id,)).fetchone()
    # La contraseña se verifica ANTES del freno: quien la sabe entra siempre. Al revés,
    # cualquiera que conociera un DNI podría dejar a esa persona afuera de su propia cuenta.
    if not row or not auth.verify_password(password, row["password_hash"]):
        intentos.fallo(login_id, ip)
        return redirect("/login", err=intentos.bloqueado(login_id, ip)
                        or "Usuario o contraseña incorrectos.")
    intentos.acierto(login_id, ip)
    # los estudiantes deshabilitados sí entran (ven su historial y el aviso administrativo)
    if row["role"] != "student" and not row["active"]:
        return redirect("/login", err="Tu usuario está deshabilitado. Hablá con la coordinación.")
    token = auth.create_session(row["id"])
    resp = redirect("/")
    resp.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="lax",
        max_age=60 * 60 * 12, path=BASE_PATH or "/",
    )
    return resp


@app.get("/clave", response_class=HTMLResponse)
def clave_pedir(request: Request):
    return render(request, "clave_pedir.html")


@app.post("/clave")
def clave_enviar(request: Request, dato: str = Form(...)):
    """Manda el enlace para elegir contraseña.

    La respuesta es siempre la misma, exista o no la cuenta: si dijera «ese DNI no está»,
    cualquiera podría averiguar quién tiene cuenta probando números de documento.
    """
    mismo = ("Si esa cuenta existe, le mandamos un correo con el enlace para elegir una "
             "contraseña nueva. Revisá también el correo no deseado.")
    fila = claves.buscar_cuenta(dato)
    if fila and (fila["email"] or "").strip():
        token = claves.crear(fila["id"], "olvido")
        if token:
            enlace = emailer.url_absoluta(f"/clave/{token}")
            emailer.enviar(fila["email"], emailer.recuperacion(
                first_name(fila["full_name"]), fila["login"], enlace))
    return redirect("/login", msg=mismo)


@app.get("/clave/{token}", response_class=HTMLResponse)
def clave_fijar(request: Request, token: str):
    fila = claves.usuario_de(token)
    if not fila:
        return redirect("/clave", err=(
            "Ese enlace ya se usó o venció. Pedí uno nuevo y usalo apenas te llegue."))
    return render(request, "clave_fijar.html", token=token, quien=fila,
                  primera=fila["motivo"] == "alta")


@app.post("/clave/{token}")
def clave_guardar(request: Request, token: str, password: str = Form(...),
                  password2: str = Form(...)):
    fila = claves.usuario_de(token)
    if not fila:
        return redirect("/clave", err="Ese enlace ya se usó o venció. Pedí uno nuevo.")
    if password != password2:
        return redirect(f"/clave/{token}", err="Las dos contraseñas no coinciden.")
    if len(password) < 8:
        return redirect(f"/clave/{token}", err="La contraseña tiene que tener al menos 8 caracteres.")
    if not claves.consumir(token, auth.hash_password(password)):
        return redirect("/clave", err="Ese enlace ya se usó o venció. Pedí uno nuevo.")
    return redirect("/login", msg="Listo, ya podés entrar con tu contraseña nueva.")


@app.post("/logout")
def logout(request: Request):
    # Las dos cookies apuntan a sesiones de la misma tabla: hay que invalidar las dos,
    # o cerrar sesión solo borra la cookie y el token queda vivo para siempre.
    for nombre in (auth.COOKIE_NAME, auth.LTI_COOKIE_NAME):
        if token := request.cookies.get(nombre):
            auth.destroy_session(token)
    resp = redirect("/login", msg="Sesión cerrada.")
    resp.delete_cookie(auth.COOKIE_NAME, path=BASE_PATH or "/")
    resp.delete_cookie(auth.LTI_COOKIE_NAME, path=BASE_PATH or "/")
    return resp


@app.get("/salud")
def salud():
    # El correo entra acá porque su falta no se nota hasta que alguien espera un enlace
    # que nunca llega: sin esto, la única forma de descubrirlo es que falle en manos de
    # una persona. `desvio` avisa si los correos se están desviando a una casilla de
    # pruebas, que en producción no debería pasar nunca.
    return {"ok": True, "modelo": model_info(),
            "correo": {"configurado": smtp_configured(), "desvio": desvio()}}


# ---------------------------------------------------------------- cuenta

@app.get("/cuenta", response_class=HTMLResponse)
def cuenta(request: Request):
    user = auth.current_user(request)
    if not user:
        return redirect("/login")
    return render(request, "cuenta.html")


@app.post("/cuenta/clave")
def cuenta_clave(
    request: Request, actual: str = Form(...), nueva: str = Form(...), repetir: str = Form(...),
):
    user = auth.current_user(request)
    if not user:
        return redirect("/login")
    if not auth.verify_password(actual, user["password_hash"]):
        return redirect("/cuenta", err="La contraseña actual no es correcta.")
    if len(nueva) < 8:
        return redirect("/cuenta", err="La contraseña nueva necesita al menos 8 caracteres.")
    if nueva != repetir:
        return redirect("/cuenta", err="Las contraseñas nuevas no coinciden.")
    if nueva == actual:
        return redirect("/cuenta", err="La contraseña nueva es igual a la actual.")
    with get_db() as db:
        db.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (auth.hash_password(nueva), user["id"]),
        )
    return redirect("/cuenta", msg="Contraseña actualizada.")


@app.post("/cuenta/consentimiento")
def cuenta_consentimiento(request: Request, consent: str = Form("")):
    """Opt-in explícito para que las entregas anonimizadas se usen en investigación educativa.

    Se puede cambiar cuantas veces se quiera y no condiciona nada del cursado: quien
    dice que no usa el sistema exactamente igual, y sus datos quedan fuera del export.
    """
    user = auth.current_user(request)
    if not user:
        return redirect("/login")
    valor = 1 if consent == "1" else 0
    with get_db() as db:
        db.execute(
            "UPDATE users SET consent = ?, consent_at = ? WHERE id = ?", (valor, utcnow(), user["id"])
        )
    return redirect("/cuenta", msg="Listo, guardamos tu decisión. Podés cambiarla cuando quieras.")


AVATAR_MIMES = {"image/jpeg", "image/png", "image/webp"}
MAX_AVATAR_BYTES = 1024 * 1024


@app.post("/cuenta/foto")
async def cuenta_foto(request: Request, foto: UploadFile | None = File(None)):
    user = auth.current_user(request)
    if not user:
        return redirect("/login")
    if not foto or not foto.filename:
        return redirect("/cuenta", err="Elegí una imagen.")
    data = await foto.read()
    if len(data) > MAX_AVATAR_BYTES:
        return redirect("/cuenta", err="La foto supera el máximo de 1 MB. Achicala y volvé a probar.")
    mime = foto.content_type if foto.content_type in AVATAR_MIMES else None
    if mime is None:
        nombre = foto.filename.lower()
        if nombre.endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif nombre.endswith(".png"):
            mime = "image/png"
        elif nombre.endswith(".webp"):
            mime = "image/webp"
    if mime is None:
        return redirect("/cuenta", err="Formato no soportado: subí una JPG, PNG o WEBP.")
    with get_db() as db:
        db.execute("UPDATE users SET avatar = ?, avatar_mime = ? WHERE id = ?", (data, mime, user["id"]))
    return redirect("/cuenta", msg="Foto actualizada.")


@app.post("/cuenta/foto/quitar")
def cuenta_foto_quitar(request: Request):
    user = auth.current_user(request)
    if not user:
        return redirect("/login")
    with get_db() as db:
        db.execute("UPDATE users SET avatar = NULL, avatar_mime = '' WHERE id = ?", (user["id"],))
    return redirect("/cuenta", msg="Foto quitada.")


@app.get("/avatar/{uid}")
def avatar(request: Request, uid: int):
    """La foto de alguien, para quien tenga algo que ver con esa persona.

    Estar logueado no alcanzaba: con el id en la dirección, cualquiera podía recorrer las
    fotos de toda la universidad. Se sirve la propia, la de cualquiera si mirás desde el
    equipo docente, y la de quien comparte una cursada con vos.
    """
    quien = auth.current_user(request)
    if not quien:
        return redirect("/login")
    if quien["id"] != uid and quien["role"] not in STAFF:
        with get_db() as db:
            juntos = db.execute(
                "SELECT 1 FROM enrollments a JOIN enrollments b ON a.edition_id = b.edition_id"
                " WHERE a.user_id = ? AND b.user_id = ? LIMIT 1", (quien["id"], uid),
            ).fetchone()
        if not juntos:
            return Response(status_code=404)
    with get_db() as db:
        row = db.execute("SELECT avatar, avatar_mime FROM users WHERE id = ?", (uid,)).fetchone()
    if not row or not row["avatar"]:
        return Response(status_code=404)
    return Response(content=row["avatar"], media_type=row["avatar_mime"],
                    headers={"Cache-Control": "private, max-age=300"})


# ---------------------------------------------------------------- estudiante

# Una cursada tiene que caer en un año plausible: el campo viene precargado con el año
# en curso, así que un valor fuera de rango es un dedazo, no una intención.
ANIO_MIN, ANIO_MAX = 2000, 2100


def _en_plataforma(tipo: str, marcado: str, fecha_cierre: str) -> tuple[int, str]:
    """¿La instancia se rinde en la plataforma? Devuelve (0/1, aviso para el docente).

    Solo tiene sentido donde hay preguntas cargadas —un examen escrito o un multiple
    choice—: un trabajo abierto se hace con las herramientas de cada quien y encerrarlo en
    un cuadro de texto no aportaría nada.

    Y exige hora de cierre. Todo el espacio se apoya en un plazo: el reloj que se ve
    mientras se rinde, y la entrega automática de lo guardado cuando se acaba. Sin cierre
    no hay ninguna de las dos, y quedaría un examen abierto para siempre.
    """
    if marcado != "1" or tipo not in ("escrito", "choice"):
        return 0, ""
    if not fecha_cierre.strip():
        return 0, (" No quedó marcado que se rinda en la plataforma: para eso hace falta "
                   "la fecha y hora de cierre, que es de donde sale el reloj del examen.")
    return 1, ""


def _leer_anio(valor, por_defecto: int | None = None) -> int | None:
    """El año que vino del formulario, o None si no es un año. Vacío = el por defecto."""
    texto = (valor or "").strip()
    if not texto:
        return por_defecto if por_defecto is not None else anio_actual()
    try:
        n = int(texto)
    except ValueError:
        return None
    return n if ANIO_MIN <= n <= ANIO_MAX else None


def _reparto_por_anio(cursadas):
    """Reparte cursadas en (visibles_por_filtro, cuántas hay de cada una).

    Lo comparten el espacio del estudiante y el listado del equipo docente: qué cuenta
    como «anterior» tiene que querer decir lo mismo en las dos pantallas. La del año que
    viene entra en «actual»: lo que se guarda para después es lo viejo, no lo que todavía
    no empezó.
    """
    este = anio_actual()
    actual = [c for c in cursadas if c["anio"] >= este]
    anteriores = [c for c in cursadas if c["anio"] < este]
    return ({"actual": actual, "anteriores": anteriores, "todas": list(cursadas)},
            {"actual": len(actual), "anteriores": len(anteriores), "todas": len(cursadas)})


def _filtro_elegido(db, user, ver: str) -> str:
    """El filtro que corresponde mostrar, guardando el que la persona acaba de elegir."""
    guardado = user["panel_filtro"] if "panel_filtro" in user.keys() else ""
    if ver in FILTROS_PANEL:
        if ver != guardado:
            db.execute("UPDATE users SET panel_filtro = ? WHERE id = ?", (ver, user["id"]))
        return ver
    return guardado if guardado in FILTROS_PANEL else FILTRO_POR_DEFECTO


# Los filtros del espacio del estudiante. Se guarda el modo elegido y nunca un año
# concreto: en 2027, «el año en curso» tiene que seguir queriendo decir 2027.
FILTROS_PANEL = ("actual", "anteriores", "todas")
FILTRO_POR_DEFECTO = "actual"


@app.get("/panel", response_class=HTMLResponse)
def panel_root(request: Request, ver: str = ""):
    user, resp = _require(request, "student")
    if resp:
        return resp
    with get_db() as db:
        cursos = student_editions(db, user["id"])
        if len(cursos) == 1:
            return redirect(f"/panel/{cursos[0]['id']}")
        filtro = _filtro_elegido(db, user, ver)
        cfg = get_config(db)
        por_filtro, cuantas = _reparto_por_anio(cursos)
        items = [{
            "c": c,
            "n_instancias": len(edition_assignments(db, c["id"], only_active=True)),
        } for c in por_filtro[filtro]]
    return render(request, "panel_cursos.html", items=items, cfg=cfg,
                  filtro=filtro, cuantas=cuantas, anio=anio_actual())


@app.get("/panel/{cid}", response_class=HTMLResponse)
def panel_curso(request: Request, cid: int):
    user, resp = _require(request, "student")
    if resp:
        return resp
    with get_db() as db:
        course = get_edition(db, cid)
        # Una cursada cerrada se sigue viendo: es lo que promete su tarjeta, y con una
        # sola cursada cerrada rebotar a /panel armaba un bucle de redirecciones, porque
        # /panel vuelve a entrar a la única que hay. Entregar ya está bloqueado en las
        # rutas de entrega, que sí miran si la cursada está abierta.
        if not course or not is_enrolled(db, user["id"], cid):
            return redirect("/panel")
        cfg = get_config(db)
        items = []
        for a in edition_assignments(db, cid, only_active=True):
            usadas = practicas_usadas(db, user["id"], a["id"])
            items.append({
                "a": a,
                "restantes": max(0, a["max_practicas"] - usadas),
                "final": final_activa(db, user["id"], a["id"]),
            })
        multi = len(student_editions(db, user["id"])) > 1
    return render(request, "panel_curso.html", course=course, items=items, cfg=cfg, multi=multi)


@app.get("/panel/instancia/{aid}", response_class=HTMLResponse)
def panel_instancia(request: Request, aid: int):
    user, resp = _require(request, "student")
    if resp:
        return resp
    with get_db() as db:
        assignment = get_assignment(db, aid)
        if not assignment or not assignment["active"]:
            return redirect("/panel")
        course = get_edition(db, assignment["edition_id"])
        if not is_enrolled(db, user["id"], course["id"]):
            return redirect("/panel")
        cfg = get_config(db)
        usadas = practicas_usadas(db, user["id"], aid)
        final = final_activa(db, user["id"], aid)
        g = grupo_de(db, user["id"], aid)
        if g:
            entregas = db.execute(
                "SELECT s.*, u.full_name AS autor FROM submissions s JOIN users u ON u.id = s.user_id"
                " WHERE s.assignment_id = ? AND s.user_id IN"
                " (SELECT user_id FROM grupo_miembros WHERE grupo_id = ?) ORDER BY s.id DESC",
                (aid, g["id"]),
            ).fetchall()
        else:
            entregas = db.execute(
                "SELECT *, NULL AS autor FROM submissions WHERE user_id = ? AND assignment_id = ? ORDER BY id DESC",
                (user["id"], aid),
            ).fetchall()
        items = assignment_items(db, aid)
        habilitada = inscripcion_habilitada(db, user["id"], course["id"])
        # La última versión ya evaluada: es lo que se puede marcar como definitiva. La
        # definitiva no se sube aparte —sería cargar el mismo trabajo dos veces—, se
        # elige entre lo que ya recibió devolución.
        ultima = db.execute(
            "SELECT * FROM submissions WHERE user_id = ? AND assignment_id = ?"
            " AND kind = 'practica' AND COALESCE(ai_feedback_md, '') != ''"
            " ORDER BY id DESC LIMIT 1", (user["id"], aid),
        ).fetchone()
        grupo = grupo_de(db, user["id"], aid)
        companeros = [m for m in miembros_de(db, grupo["id"]) if m["id"] != user["id"]] if grupo else []
        grupo_cerrado = bool(grupo) and bool(db.execute(
            "SELECT 1 FROM submissions WHERE assignment_id = ? AND user_id IN "
            "(SELECT user_id FROM grupo_miembros WHERE grupo_id = ?)", (aid, grupo["id"])
        ).fetchone())
        # Si ya entró al examen alguna vez, el botón dice «Continuar» y no «Comenzar»:
        # importa saber que lo que había escrito sigue ahí.
        empezado = bool(assignment["en_plataforma"]
                        and examen_mod.borrador(db, user["id"], aid))
    maxp = assignment["max_practicas"]
    abierta, motivo_cierre = ventana_entrega(assignment)
    if not course["active"]:
        # La cursada terminó: se consulta, no se entrega. Se dice por el mismo camino que
        # el plazo vencido para que la pantalla no tenga que aprender un estado más.
        abierta = False
        motivo_cierre = "Esta cursada está cerrada."
    # En el multiple choice se responde marcando la opción, en la misma lista de preguntas.
    # Solo mientras se pueda entregar: después esa lista vuelve a ser algo para leer. Si se
    # rinde en la plataforma, no se responde acá: se responde en el espacio del examen.
    respondible = bool(assignment["tipo"] == "choice" and not final and abierta and habilitada
                       and not assignment["en_plataforma"])
    return render(
        request, "panel.html", cfg=cfg, course=course, assignment=assignment,
        usadas=usadas, maxp=maxp, restantes=max(0, maxp - usadas), final=final,
        entregas=entregas, items=items, puntaje_total=items_puntaje_total(items),
        grupo=grupo, companeros=companeros, grupo_cerrado=grupo_cerrado,
        ventana_abierta=abierta, motivo_cierre=motivo_cierre,
        respondible=respondible, ultima=ultima, empezado=empezado,
    )


# ---------------------------------------------------------------- grupos de TP

@app.post("/panel/instancia/{aid}/grupo")
async def panel_grupo(request: Request, aid: int):
    """El estudiante arma su grupo cargando los DNI de sus compañeros, o se va del grupo.

    Un grupo no se puede tocar una vez que tiene entregas: el cupo y la devolución
    ya son del conjunto, y cambiar quién lo integra reescribiría a quién le contaron.
    """
    user, resp = _require(request, "student")
    if resp:
        return resp
    form = await request.form()
    volver = f"/panel/instancia/{aid}"
    with get_db() as db:
        assignment = get_assignment(db, aid)
        if not assignment or not assignment["active"]:
            return redirect("/panel")
        ed = get_edition(db, assignment["edition_id"])
        if not ed["active"] or not is_enrolled(db, user["id"], ed["id"]):
            return redirect("/panel")
        if assignment["max_integrantes"] < 2:
            return redirect(volver, err="Esta instancia es de entrega individual.")
        if not inscripcion_habilitada(db, user["id"], ed["id"]):
            return redirect(volver, err="Tu inscripción a esta cursada está deshabilitada.")

        actual = grupo_de(db, user["id"], aid)
        if actual and db.execute(
            "SELECT 1 FROM submissions WHERE assignment_id = ? AND user_id IN "
            "(SELECT user_id FROM grupo_miembros WHERE grupo_id = ?)", (aid, actual["id"])
        ).fetchone():
            return redirect(volver, err="El grupo ya tiene entregas: para cambiarlo, hablá con el equipo docente.")

        if form.get("action") == "salir":
            if not actual:
                return redirect(volver)
            db.execute("DELETE FROM grupo_miembros WHERE grupo_id = ? AND user_id = ?", (actual["id"], user["id"]))
            if not miembros_de(db, actual["id"]):
                db.execute("DELETE FROM grupos WHERE id = ?", (actual["id"],))
            return redirect(volver, msg="Saliste del grupo: volvés a entregar por tu cuenta.")

        # armar o rehacer el grupo con los DNI que cargó
        dnis = [d.strip() for d in (form.get("companeros") or "").replace(";", ",").replace("\n", ",").split(",")]
        dnis = [re.sub(r"[.\s]", "", d) for d in dnis if d.strip()]
        if not dnis:
            return redirect(volver, err="Cargá el DNI de al menos un compañero o compañera.")

        companeros, errores = [], []
        for dni in dnis:
            fila = db.execute("SELECT * FROM users WHERE login = ? AND role = 'student'", (dni,)).fetchone()
            if not fila:
                errores.append(f"{dni} no figura como estudiante")
            elif fila["id"] == user["id"]:
                continue
            elif not is_enrolled(db, fila["id"], ed["id"]):
                errores.append(f"{fila['full_name']} no está en esta cursada")
            elif grupo_de(db, fila["id"], aid):
                errores.append(f"{fila['full_name']} ya está en otro grupo")
            else:
                companeros.append(fila)
        if errores:
            return redirect(volver, err=" · ".join(errores))
        if len(companeros) + 1 > assignment["max_integrantes"]:
            return redirect(volver, err=f"El máximo es de {assignment['max_integrantes']} integrantes por grupo.")

        if actual:
            db.execute("DELETE FROM grupo_miembros WHERE grupo_id = ?", (actual["id"],))
            gid = actual["id"]
        else:
            gid = db.execute(
                "INSERT INTO grupos (assignment_id, created_at) VALUES (?, ?)", (aid, utcnow())
            ).lastrowid
        for u in [user, *companeros]:
            db.execute(
                "INSERT INTO grupo_miembros (grupo_id, user_id, assignment_id) VALUES (?, ?, ?)",
                (gid, u["id"], aid),
            )
        nombres = ", ".join(u["full_name"] for u in companeros)
    return redirect(volver, msg=f"Grupo armado con {nombres}. El cupo y la devolución son del grupo.")


# ---------------------------------------------------------------- examen en plataforma
#
# El desarrollo del examen ocurre acá adentro: se escribe o se marca en una pantalla
# propia, con reloj, y lo escrito se guarda en el servidor mientras se trabaja. La entrega
# se arma con lo guardado y sigue el mismo circuito que cualquier otra: no hay un camino
# paralelo de corrección ni de calificación, solo otra forma de llegar.


def _examen_contexto(db, user, aid):
    """(assignment, course, items) si esta persona puede rendir acá, o (None, None, None)."""
    assignment = get_assignment(db, aid)
    if not assignment or not assignment["active"] or not assignment["en_plataforma"]:
        return None, None, None
    course = get_edition(db, assignment["edition_id"])
    if not course or not is_enrolled(db, user["id"], course["id"]):
        return None, None, None
    return assignment, course, assignment_items(db, aid)


@app.get("/examen/{aid}", response_class=HTMLResponse)
def examen(request: Request, aid: int):
    """El espacio donde se rinde."""
    user, resp = _require(request, "student")
    if resp:
        return resp
    with get_db() as db:
        assignment, course, items = _examen_contexto(db, user, aid)
        if not assignment:
            return redirect("/panel")
        volver = f"/panel/instancia/{aid}"
        final = final_activa(db, user["id"], aid)
        if final:
            return redirect(f"/entrega/{final['id']}")
        if not course["active"]:
            return redirect(volver, err="Esta cursada está cerrada.")
        if not inscripcion_habilitada(db, user["id"], course["id"]):
            return redirect(volver, err="Tu inscripción a esta cursada está deshabilitada.")
        borrador = examen_mod.borrador(db, user["id"], aid)
        cfg = assignment_cfg(db, course, assignment)

    ahora, cierre = ahora_local(), momento_cierre(assignment)
    apertura = momento_apertura(assignment)
    if apertura and ahora < apertura:
        return redirect(volver, err=f"Este examen abre el {fecha_corta(assignment['fecha_apertura'])}.")

    # Cerró mientras no estaba mirando: lo que vale es lo que quedó guardado en el
    # servidor, y se entrega ahora aunque vuelva al día siguiente. Si no alcanzó a
    # escribir nada, no se le inventa una entrega en blanco.
    if cierre and ahora > cierre:
        respuestas = examen_mod.respuestas_de(borrador)
        if borrador and examen_mod.hay_algo(items, respuestas, assignment["tipo"]):
            return _examen_cerrar(user, assignment, course, items, cfg, respuestas, vencido=True)
        return redirect(volver, err=f"El plazo venció el {fecha_corta(assignment['fecha_cierre'])}.")

    with get_db() as db:
        borrador = examen_mod.abrir(db, user["id"], aid)
    return render(
        request, "examen.html", assignment=assignment, course=course, items=items,
        puntaje_total=items_puntaje_total(items),
        respuestas=examen_mod.respuestas_de(borrador),
        cierre=cierre, ahora=ahora, guardado=borrador["guardado_at"],
    )


def _examen_abierto(db, user, aid):
    """El examen en curso de esta persona, o None si no lo tiene abierto.

    Lo comparten el guardado y el registro de incidentes: los dos llegan por detrás de la
    pantalla y ninguno puede confiar en que la pantalla siga siendo la que se sirvió.
    """
    assignment, course, items = _examen_contexto(db, user, aid)
    if not assignment or not course["active"]:
        return None, None, None
    if not inscripcion_habilitada(db, user["id"], course["id"]):
        return None, None, None
    if final_activa(db, user["id"], aid) or not examen_mod.borrador(db, user["id"], aid):
        return None, None, None
    return assignment, course, items


@app.post("/examen/{aid}/guardar")
async def examen_guardar(request: Request, aid: int):
    """Autoguardado de lo escrito. Lo llama la pantalla sola, cada pocos segundos."""
    user, resp = _require(request, "student")
    if resp:
        return JSONResponse({"ok": False}, status_code=401)
    datos = await request.json()
    with get_db() as db:
        assignment, _course, items = _examen_abierto(db, user, aid)
        if not assignment:
            return JSONResponse({"ok": False, "motivo": "cerrado"}, status_code=409)
        cierre = momento_cierre(assignment)
        # Después del cierre se acepta un rato más: entre que se teclea la última palabra
        # y que el guardado llega, pasan segundos, y perderlos sería perder respuestas.
        if cierre and ahora_local() > _mas_gracia(cierre):
            return JSONResponse({"ok": False, "motivo": "vencido"}, status_code=409)
        respuestas = {}
        validos = {it["orden"] for it in items}
        for k, v in (datos.get("respuestas") or {}).items():
            try:
                n = int(k)
            except (TypeError, ValueError):
                continue
            if n in validos:
                respuestas[n] = str(v)[:20000]
        examen_mod.guardar(db, user["id"], aid, respuestas)
    return JSONResponse({"ok": True, "guardado": utcnow()})


@app.post("/examen/{aid}/incidente")
async def examen_incidente(request: Request, aid: int):
    """Deja constancia de algo que pasó mientras rendía, y devuelve qué avisarle."""
    user, resp = _require(request, "student")
    if resp:
        return JSONResponse({"ok": False}, status_code=401)
    datos = await request.json()
    tipo = str(datos.get("tipo") or "")
    detalle = datos.get("detalle") if isinstance(datos.get("detalle"), dict) else {}
    with get_db() as db:
        assignment, _course, _items = _examen_abierto(db, user, aid)
        if not assignment or tipo not in examen_mod.TIPOS:
            return JSONResponse({"ok": False}, status_code=409)
        cuantas = examen_mod.registrar(db, user["id"], aid, tipo, detalle)
    return JSONResponse({"ok": True, "aviso": _aviso_incidente(tipo, cuantas)})


def _aviso_incidente(tipo: str, cuantas: int) -> str:
    """Lo que se le dice a quien rinde, en el momento. Sin acusar y sin esconder nada."""
    veces = "" if cuantas <= 1 else f" (van {cuantas})"
    if tipo == "salida":
        que = f"Quedó registrado que saliste de la pantalla del examen{veces}."
    else:
        que = f"Quedó registrado que pegaste texto desde otro lado{veces}."
    return que + " El equipo docente lo va a ver junto a tu entrega."


@app.post("/examen/{aid}/entregar")
async def examen_entregar(request: Request, aid: int):
    """Entrega lo guardado. La pantalla también la llama sola cuando se acaba el tiempo."""
    user, resp = _require(request, "student")
    if resp:
        return redirect("/login")
    with get_db() as db:
        assignment, course, items = _examen_contexto(db, user, aid)
        if not assignment:
            return redirect("/panel")
        volver = f"/panel/instancia/{aid}"
        if final_activa(db, user["id"], aid):
            return redirect(volver, err="Ya tenés una entrega registrada en esta instancia.")
        if not course["active"] or not inscripcion_habilitada(db, user["id"], course["id"]):
            return redirect(volver, err="No podés entregar en esta cursada.")
        borrador = examen_mod.borrador(db, user["id"], aid)
        cfg = assignment_cfg(db, course, assignment)
    respuestas = examen_mod.respuestas_de(borrador)
    if not borrador or not examen_mod.hay_algo(items, respuestas, assignment["tipo"]):
        return redirect(f"/examen/{aid}", err="Todavía no hay nada escrito para entregar.")
    cierre = momento_cierre(assignment)
    if cierre and ahora_local() > _mas_gracia(cierre):
        # Vencido: se entrega igual, porque lo guardado es del estudiante y ya estaba
        # antes de la hora. Lo que no se acepta después del cierre es escribir más.
        return _examen_cerrar(user, assignment, course, items, cfg, respuestas, vencido=True)
    return _examen_cerrar(user, assignment, course, items, cfg, respuestas, vencido=False)


def _mas_gracia(cierre: str) -> str:
    """El cierre más los segundos de gracia, en el mismo formato de texto comparable."""
    from datetime import timedelta
    momento = datetime.strptime(cierre, "%Y-%m-%dT%H:%M")
    return (momento + timedelta(seconds=examen_mod.GRACIA_SEGUNDOS)).strftime("%Y-%m-%dT%H:%M")


def _examen_cerrar(user, assignment, course, items, cfg, respuestas, vencido: bool):
    """Convierte lo guardado en una entrega y la hace pasar por el circuito de siempre."""
    aid = assignment["id"]
    tipo = assignment["tipo"]
    marcadas = examen_mod.marcadas_de(items, respuestas) if tipo == "choice" else None
    work_text = ("\n".join(f"{n}-{marcadas[n]}" for n in sorted(marcadas)) if marcadas
                 else examen_mod.texto_de(items, respuestas))
    kind = "practica" if assignment["max_practicas"] else "final"

    tele, nota_calculada, niveles = {}, None, []
    try:
        if tipo == "choice":
            feedback, model, tele, nota_calculada = _corregir_choice(
                cfg, first_name(user["full_name"]), work_text, marcadas)
        else:
            feedback, model, tele = generate_feedback(
                cfg, first_name(user["full_name"]), user["profile"] or "", work_text, kind,
                False, [])
        feedback, niveles = separar_niveles(feedback, criterios_de(cfg.get("rubrica", "")))
        status = "pendiente" if kind == "final" else "ok"
        error = ""
    except LLMError as exc:
        if kind != "final":
            return redirect(f"/examen/{aid}", err=f"No se pudo generar la devolución. {exc}")
        feedback, model, status, error = "", "", "pendiente", str(exc)

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO submissions (user_id, assignment_id, kind, status, original_filename,"
            " work_text, text_chars, truncated, ai_feedback_md, model_used, error, created_at,"
            " cfg_snapshot, tokens_in, tokens_out, latencia_ms, finish_reason, nota, niveles)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], aid, kind, status, "rendido en la plataforma", work_text,
             len(work_text), feedback, model, error, utcnow(),
             json.dumps(cfg, ensure_ascii=False), tele.get("tokens_in"), tele.get("tokens_out"),
             tele.get("latencia_ms"), tele.get("finish_reason"), nota_calculada,
             json.dumps(niveles, ensure_ascii=False) if niveles else ""),
        )
        sid = cur.lastrowid
        examen_mod.colgar_de_entrega(db, user["id"], aid, sid)
        if kind == "final":
            examen_mod.borrar(db, user["id"], aid)

    if kind != "final":
        return redirect(f"/entrega/{sid}")
    firma_sola = not assignment["requiere_revision"] and not error
    nota = _asentar_nota(sid, cfg, firma_sola)
    entregada = ("Se cerró tu examen al vencer el tiempo, con lo que tenías escrito."
                 if vencido else "Examen entregado.")
    if error:
        return redirect(f"/entrega/{sid}", msg=(
            entregada + " No se pudo generar la devolución en este momento: la va a revisar "
            "el equipo docente."))
    if firma_sola:
        cuanto = f" Tu calificación es {_num_nota(nota)}." if nota is not None else ""
        return redirect(f"/entrega/{sid}", msg=entregada + cuanto)
    return redirect(f"/entrega/{sid}", msg=entregada + " Queda en revisión del equipo docente.")


@app.post("/entregar")
async def entregar(
    request: Request,
    kind: str = Form(...),
    assignment_id: int = Form(...),
    archivo: UploadFile | None = File(None),
    texto: str = Form(""),
    origen: str = Form(""),
    fotos_n: int = Form(0),
    propuesta: UploadFile | None = File(None),
    repo: str = Form(""),
):
    user, resp = _require(request, "student")
    if resp:
        return resp
    if kind not in ("practica", "final"):
        return redirect("/panel", err="Tipo de entrega inválido.")
    back = f"/panel/instancia/{assignment_id}"
    # En el multiple choice la respuesta llega marcada, un campo por pregunta. Se leen del
    # formulario crudo porque son tantas como preguntas tenga el examen.
    formulario = await request.form()
    marcadas = {}
    for clave, valor in formulario.multi_items():
        if clave.startswith("resp_") and clave[5:].isdigit() and str(valor).strip():
            marcadas[int(clave[5:])] = str(valor).strip().lower()[:1]

    with get_db() as db:
        assignment = get_assignment(db, assignment_id)
        course = get_edition(db, assignment["edition_id"]) if assignment else None
        if (not assignment or not assignment["active"] or not course["active"]
                or not is_enrolled(db, user["id"], course["id"])):
            return redirect("/panel", err="Esa instancia de evaluación no está disponible.")
        if not inscripcion_habilitada(db, user["id"], course["id"]):
            return redirect(back, err=(
                "Tu inscripción a esta cursada está deshabilitada, así que no podés presentar "
                "nada nuevo acá. Hablá con el equipo docente de la cursada."))
        cfg = assignment_cfg(db, course, assignment)
        # La definitiva se sube directo solo cuando la instancia no admite versiones
        # previas. Si las admite, nace de marcar una que ya recibió devolución: así lo
        # que se firma es exactamente lo que Lidia evaluó, y no una segunda carga.
        if kind == "final" and assignment["max_practicas"]:
            return redirect(back, err="La entrega definitiva se marca desde una versión ya corregida.")
        abierta, motivo = ventana_entrega(assignment)
        if not abierta:
            return redirect(back, err=motivo)
        if kind == "practica" and not assignment["max_practicas"]:
            return redirect(back, err="Esta evaluación se entrega una sola vez: no admite versiones previas.")
        if kind == "practica" and practicas_usadas(db, user["id"], assignment_id) >= assignment["max_practicas"]:
            return redirect(back, err="Ya usaste todas tus devoluciones. Podés presentar la entrega definitiva.")
        if kind == "final" and final_activa(db, user["id"], assignment_id):
            return redirect(back, err="Ya tenés una entrega final en curso.")
        if assignment["modalidad"] == "papel" and origen != "foto":
            return redirect(back, err="Esta instancia se entrega en papel: subí las fotos de tu hoja.")

    # respuestas de un multiple choice son cortas por naturaleza; una transcripción
    # de examen en papel ya pasó por la confirmación del estudiante
    min_len = 3 if assignment["tipo"] == "choice" else (50 if origen == "foto" else 200)
    filename = ""
    data = b""
    imagenes = 0
    paginas, total_paginas = [], 0
    try:
        if archivo and archivo.filename:
            data = await archivo.read()
            filename = archivo.filename
            if assignment["usa_vision"] and not filename.lower().endswith(".pdf"):
                return redirect(back, err=(
                    "Esta instancia se corrige mirando el documento, así que la entrega tiene "
                    "que ser un PDF. Exportá tu trabajo a PDF y volvé a subirlo."))
            work_text, truncated = extract_text(filename, data)
            imagenes = contar_imagenes(filename, data)
            if assignment["usa_vision"]:
                # Las páginas reemplazan al texto: mandar las dos cosas sería pagar dos
                # veces por el mismo contenido.
                paginas, total_paginas = paginas_de_pdf(data)
        elif texto.strip():
            work_text, truncated = texto.strip(), False
            if origen == "foto":
                filename = f"examen en papel ({fotos_n} foto{'s' if fotos_n != 1 else ''})"
            if len(work_text) < min_len:
                return redirect(back, err="Lo entregado es demasiado corto para evaluarlo.")
        elif marcadas:
            # Un multiple choice respondido marcando no tiene archivo ni texto: lo que
            # entregó son las opciones elegidas. Se guardan legibles para que la entrega
            # se pueda leer después sin necesitar la aplicación.
            work_text = "\n".join(f"{n}-{marcadas[n]}" for n in sorted(marcadas))
            truncated = False
            filename = "respuestas marcadas"
        else:
            return redirect(back, err="Elegí el archivo de tu trabajo antes de entregar.")
    except ExtractionError as exc:
        return redirect(back, err=str(exc))

    # Alcance acordado: la propuesta ya aprobada, que el estudiante sube junto con el
    # trabajo. Si la instancia la pide y no viene, se corrige igual y queda marcado.
    propuesta_text = ""
    if assignment["pide_propuesta"] and propuesta and propuesta.filename:
        try:
            propuesta_text, _ = extract_text(propuesta.filename, await propuesta.read())
        except ExtractionError as exc:
            return redirect(back, err=f"No se pudo leer la propuesta adjunta: {exc}")
    sin_propuesta = bool(assignment["pide_propuesta"] and not propuesta_text.strip())
    cfg["propuesta"] = propuesta_text

    # El repositorio que el estudiante declaró. Si falla se avisa y no se corrige: el
    # enlace roto es del estudiante y lo puede arreglar, y corregir sin el código cuando
    # la instancia lo pide daría una devolución sobre la mitad del trabajo.
    repo_url = (repo or "").strip()
    repo_resumen = ""
    if assignment["pide_repo"] and repo_url:
        try:
            cfg["repo_texto"], resumen = repos.traer(repo_url)
            repo_resumen = json.dumps(resumen, ensure_ascii=False)
        except repos.RepoError as exc:
            return redirect(back, err=f"No se pudo leer el repositorio: {exc}")

    # Revisión de integridad: una pasada aparte, solo sobre el material del estudiante.
    # Si el mismo llamado que puede ser manipulado fuera el que reporta la manipulación,
    # el reporte no valdría nada.
    alerta = revisar_integridad("\n\n".join(
        x for x in (work_text, propuesta_text, cfg.get("repo_texto", "")) if x))

    tele = {}
    nota_calculada = None
    niveles = []
    try:
        if assignment["tipo"] == "choice":
            feedback, model, tele, nota_calculada = _corregir_choice(
                cfg, first_name(user["full_name"]), work_text, marcadas)
        else:
            if paginas:
                cfg = {**cfg, "por_imagen": True, "paginas_totales": total_paginas,
                       "paginas_enviadas": len(paginas),
                       "paginas_omitidas": max(0, total_paginas - len(paginas))}
            feedback, model, tele = generate_feedback(
                cfg, first_name(user["full_name"]), user["profile"] or "", work_text, kind,
                truncated, paginas
            )
        # Los niveles por criterio vienen al final de la devolución, en un bloque aparte:
        # se guardan como dato y se quitan del texto que lee la persona.
        feedback, niveles = separar_niveles(feedback, criterios_de(cfg.get("rubrica", "")))
        status = "pendiente" if kind == "final" else "ok"
        error = ""
    except LLMError as exc:
        if kind == "final":
            # La entrega se registra igual —perderla por un fallo del modelo sería mucho
            # peor—, pero queda esperando a una persona, aunque la instancia no lleve
            # firma: cerrarla sola dejaría una entrega aprobada con la devolución vacía y
            # sin que nadie se entere.
            feedback, model, status, error = "", "", "pendiente", str(exc)
        else:
            return redirect(back, err=f"No se pudo generar la devolución (no se consumió tu intento). {exc}")

    with get_db() as db:
        grupo = grupo_de(db, user["id"], assignment_id)
        cur = db.execute(
            "INSERT INTO submissions (user_id, assignment_id, kind, status, original_filename, work_text,"
            " text_chars, truncated, ai_feedback_md, model_used, error, created_at,"
            " grupo_id, cfg_snapshot, tokens_in, tokens_out, latencia_ms, finish_reason,"
            " propuesta_text, sin_propuesta, repo_url, repo_resumen, alerta, nota, niveles, imagenes,"
            " paginas, paginas_vistas)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], assignment_id, kind, status, filename, work_text, len(work_text), int(truncated),
             feedback, model, error, utcnow(),
             grupo["id"] if grupo else None, json.dumps(cfg, ensure_ascii=False),
             tele.get("tokens_in"), tele.get("tokens_out"), tele.get("latencia_ms"),
             tele.get("finish_reason"), propuesta_text, int(sin_propuesta),
             repo_url, repo_resumen, alerta,
             # En el multiple choice la nota viene calculada y queda propuesta en la ficha;
             # el docente la confirma o la cambia, igual que antes. En el resto va vacía.
             nota_calculada,
             json.dumps(niveles, ensure_ascii=False) if niveles else "", imagenes,
             total_paginas, len(paginas)),
        )

        # El archivo se guarda recién ahora: la carpeta se nombra con el id de la entrega,
        # que no existe hasta que la fila está insertada.
        if data and filename:
            try:
                guardado = archivos.guardar(cur.lastrowid, filename, data)
                db.execute("UPDATE submissions SET archivo_ruta = ?, archivo_bytes = ?,"
                           " archivo_sha256 = ? WHERE id = ?",
                           (guardado["ruta"], guardado["bytes"], guardado["sha256"], cur.lastrowid))
            except OSError:
                # Sin espacio o sin permisos: la entrega vale igual, se pierde el original.
                pass
        sid = cur.lastrowid
    if kind == "final":
        # Es la única entrega que admite la instancia, así que el circuito se cierra acá
        # mismo: se calcula la calificación y, si nadie tiene que firmarla, queda firme.
        firma_sola = not assignment["requiere_revision"] and not error
        nota = _asentar_nota(sid, cfg, firma_sola)
        if firma_sola:
            cuanto = f" Tu calificación es {_num_nota(nota)}." if nota is not None else ""
            return redirect(f"/entrega/{sid}", msg=(
                "Entrega registrada." + cuanto + " Esta instancia no lleva revisión docente."))
        if error:
            return redirect(f"/entrega/{sid}", msg=(
                "Entrega registrada, pero no se pudo generar la devolución. La va a revisar "
                "el equipo docente."))
        return redirect(f"/entrega/{sid}", msg="Entrega definitiva registrada: queda en revisión del equipo docente.")
    return redirect(f"/entrega/{sid}")


FOTO_MIMES = {"image/jpeg", "image/png", "image/webp"}
MAX_FOTOS = 6
MAX_FOTO_BYTES = 8 * 1024 * 1024


@app.post("/entregar/fotos")
async def entregar_fotos(
    request: Request,
    kind: str = Form(...),
    assignment_id: int = Form(...),
    fotos: list[UploadFile] = File(...),
):
    """Paso 1 del examen en papel: transcribir las fotos y mostrar la lectura.

    Acá no se registra nada: la entrega recién se confirma en el paso siguiente,
    así una foto mal sacada no consume el intento.
    """
    user, resp = _require(request, "student")
    if resp:
        return resp
    back = f"/panel/instancia/{assignment_id}"
    if kind not in ("practica", "final"):
        return redirect("/panel", err="Tipo de entrega inválido.")

    with get_db() as db:
        assignment = get_assignment(db, assignment_id)
        course = get_edition(db, assignment["edition_id"]) if assignment else None
        if course and not inscripcion_habilitada(db, user["id"], course["id"]):
            return redirect(back, err=(
                "Tu inscripción a esta cursada está deshabilitada, así que no podés presentar "
                "nada nuevo acá. Hablá con el equipo docente de la cursada."))
        if (not assignment or not assignment["active"] or not course["active"]
                or not is_enrolled(db, user["id"], course["id"])):
            return redirect("/panel", err="Esa instancia de evaluación no está disponible.")
        if assignment["modalidad"] == "digital":
            return redirect(back, err="Esta instancia se entrega en formato digital, no en papel.")
        abierta, motivo = ventana_entrega(assignment)
        if not abierta:
            return redirect(back, err=motivo)
        # La definitiva se sube directo solo cuando la instancia no admite versiones
        # previas. Si las admite, nace de marcar una que ya recibió devolución: así lo
        # que se firma es exactamente lo que Lidia evaluó, y no una segunda carga.
        if kind == "final" and assignment["max_practicas"]:
            return redirect(back, err="La entrega definitiva se marca desde una versión ya corregida.")
        if kind == "practica" and not assignment["max_practicas"]:
            return redirect(back, err="Esta evaluación se entrega una sola vez: no admite versiones previas.")
        if kind == "practica" and practicas_usadas(db, user["id"], assignment_id) >= assignment["max_practicas"]:
            return redirect(back, err="Ya usaste todas tus devoluciones. Podés presentar la entrega definitiva.")
        if kind == "final" and final_activa(db, user["id"], assignment_id):
            return redirect(back, err="Ya tenés una entrega final en curso.")

    imagenes = []
    for f in fotos:
        if not f.filename:
            continue
        data = await f.read()
        if len(data) > MAX_FOTO_BYTES:
            return redirect(back, err=f"La foto {f.filename} supera el máximo de 8 MB.")
        mime = f.content_type if f.content_type in FOTO_MIMES else None
        if mime is None:
            nombre = f.filename.lower()
            if nombre.endswith((".jpg", ".jpeg")):
                mime = "image/jpeg"
            elif nombre.endswith(".png"):
                mime = "image/png"
            elif nombre.endswith(".webp"):
                mime = "image/webp"
        if mime is None:
            return redirect(back, err=f"{f.filename}: formato no soportado. Subí fotos JPG, PNG o WEBP.")
        imagenes.append((mime, data))
    if not imagenes:
        return redirect(back, err="Elegí las fotos de tu examen.")
    if len(imagenes) > MAX_FOTOS:
        return redirect(back, err=f"Hasta {MAX_FOTOS} fotos por entrega.")

    try:
        transcripcion = transcribe_images(imagenes)
    except LLMError as exc:
        return redirect(back, err=f"No se pudo leer el examen (no se consumió tu intento). {exc}")

    return render(
        request, "confirmar_fotos.html", assignment=assignment, course=course,
        kind=kind, transcripcion=transcripcion, n_fotos=len(imagenes),
    )


async def _leer_fotos(fotos) -> list:
    """Valida tamaño, formato y cantidad. Devuelve [(mime, bytes)]. Lanza ValueError con el motivo."""
    imagenes = []
    for f in fotos:
        if not f.filename:
            continue
        data = await f.read()
        if len(data) > MAX_FOTO_BYTES:
            raise ValueError(f"La foto {f.filename} supera el máximo de 8 MB.")
        mime = f.content_type if f.content_type in FOTO_MIMES else None
        if mime is None:
            nombre = f.filename.lower()
            if nombre.endswith((".jpg", ".jpeg")):
                mime = "image/jpeg"
            elif nombre.endswith(".png"):
                mime = "image/png"
            elif nombre.endswith(".webp"):
                mime = "image/webp"
        if mime is None:
            raise ValueError(f"{f.filename}: formato no soportado. Subí fotos JPG, PNG o WEBP.")
        imagenes.append((mime, data))
    if not imagenes:
        raise ValueError("Elegí las fotos del examen.")
    if len(imagenes) > MAX_FOTOS:
        raise ValueError(f"Hasta {MAX_FOTOS} fotos por entrega.")
    return imagenes


@app.get("/entrega/{sid}", response_class=HTMLResponse)
def entrega(request: Request, sid: int):
    user = auth.current_user(request)
    if not user:
        return redirect("/login")
    with get_db() as db:
        sub, assignment, course = _load_submission(db, sid)
        if not sub:
            return redirect("/")
        if user["role"] == "student":
            if sub["user_id"] != user["id"]:
                g = grupo_de(db, user["id"], sub["assignment_id"])
                mismo_grupo = bool(g) and bool(db.execute(
                    "SELECT 1 FROM grupo_miembros WHERE grupo_id = ? AND user_id = ?",
                    (g["id"], sub["user_id"]),
                ).fetchone()) if g else False
                if not mismo_grupo:
                    return redirect("/")
        elif not can_access_edition(db, user, course["id"]):
            return redirect("/")
        qs = db.execute(
            "SELECT * FROM questions WHERE submission_id = ? ORDER BY id", (sid,)
        ).fetchall()
        owner = db.execute("SELECT * FROM users WHERE id = ?", (sub["user_id"],)).fetchone()
        incidentes = _incidentes(db, sub)
        # primera vez que el estudiante abre su devolución: dice si la leyó, y cuándo
        if user["role"] == "student" and not sub["first_viewed_at"] and sub["ai_feedback_md"]:
            db.execute("UPDATE submissions SET first_viewed_at = ? WHERE id = ?", (utcnow(), sub["id"]))
        habilitada = inscripcion_habilitada(db, user["id"], course["id"])
        # ¿Puede presentar ESTA práctica como final, tal cual está? Se calcula acá adentro,
        # con la conexión abierta.
        abierta_p, _ = ventana_entrega(assignment)
        puede_promover = bool(
            user["role"] == "student" and sub["kind"] == "practica" and sub["ai_feedback_md"]
            and habilitada and abierta_p
            and not final_activa(db, user["id"], assignment["id"])
        )
    maxq = assignment["max_preguntas"]
    puede_preguntar = (
        user["role"] == "student" and sub["kind"] == "practica" and len(qs) < maxq
        and habilitada
    )
    return render(
        request, "entrega.html", sub=sub, owner=owner, course=course, assignment=assignment,
        qs=qs, maxq=maxq, q_restantes=max(0, maxq - len(qs)), puede_preguntar=puede_preguntar,
        puede_promover=puede_promover, niveles=_niveles(sub),
        detalle_nota=_detalle_nota(sub), incidentes=incidentes,
    )


@app.get("/entrega/{sid}/archivo")
def entrega_archivo(request: Request, sid: int):
    """Descarga el documento original de una entrega.

    Lo puede bajar su autor y el equipo docente de esa cursada. Es lo que permite verificar
    que el texto sobre el que se corrigió es de verdad lo que la persona entregó.
    """
    user = auth.current_user(request)
    if not user:
        return redirect("/login")
    with get_db() as db:
        sub, assignment, course = _load_submission(db, sid)
        if not sub:
            return redirect("/")
        propio = sub["user_id"] == user["id"]
        del_equipo = user["role"] in STAFF and can_access_edition(db, user, course["id"])
        if not (propio or del_equipo):
            return redirect("/")
    ruta = archivos.ruta_absoluta(sub["archivo_ruta"] if "archivo_ruta" in sub.keys() else "")
    if not ruta:
        return redirect(f"/entrega/{sid}", err=(
            "Esta entrega no tiene el archivo original guardado: es anterior a que "
            "empezáramos a conservarlos, o se entregó pegando el texto."))
    nombre = sub["original_filename"] or "entrega"
    return FileResponse(ruta, filename=nombre)


@app.post("/entrega/{sid}/final")
def promover_a_final(request: Request, sid: int):
    """Presenta una versión ya corregida como entrega definitiva.

    El circuito es el mismo en las tres modalidades. Que la instancia lleve revisión humana
    o no cambia únicamente QUIÉN cierra: con revisión, la entrega entra a la cola del equipo
    docente y la nota queda propuesta; sin revisión, se cierra acá y la nota queda firme.
    No tener revisión no significa no tener entrega definitiva.
    """
    user, resp = _require(request, "student")
    if resp:
        return resp
    volver = f"/entrega/{sid}"
    with get_db() as db:
        sub, assignment, course = _load_submission(db, sid)
        if not sub or sub["user_id"] != user["id"] or sub["kind"] != "practica":
            return redirect("/panel")
        if not sub["ai_feedback_md"]:
            return redirect(volver, err="Esta entrega todavía no tiene devolución.")
        if not inscripcion_habilitada(db, user["id"], course["id"]):
            return redirect(volver, err="Tu inscripción a esta cursada está deshabilitada.")
        abierta, motivo = ventana_entrega(assignment)
        if not abierta:
            return redirect(volver, err=motivo or "La entrega está cerrada.")
        if final_activa(db, user["id"], assignment["id"]):
            return redirect(volver, err="Ya tenés una entrega definitiva registrada en esta instancia.")

        cfg = assignment_cfg(db, course, assignment)
        grupo = grupo_de(db, user["id"], assignment["id"])
        firma_sola = not assignment["requiere_revision"]
        estado = "aprobada" if firma_sola else "pendiente"
        nueva_id = db.execute(
            "INSERT INTO submissions (user_id, assignment_id, kind, status, original_filename,"
            " work_text, text_chars, truncated, ai_feedback_md, model_used, error, created_at,"
            " grupo_id, cfg_snapshot, propuesta_text, sin_propuesta, repo_url, repo_resumen,"
            " alerta, niveles, imagenes, paginas, paginas_vistas, archivo_ruta, archivo_bytes,"
            " archivo_sha256, promovida_de)"
            " SELECT user_id, assignment_id, 'final', ?, original_filename,"
            " work_text, text_chars, truncated, ai_feedback_md, model_used, error, ?,"
            " ?, cfg_snapshot, propuesta_text, sin_propuesta, repo_url, repo_resumen,"
            " alerta, niveles, imagenes, paginas, paginas_vistas, archivo_ruta, archivo_bytes,"
            " archivo_sha256, id FROM submissions WHERE id = ?",
            (estado, utcnow(), grupo["id"] if grupo else None, sid),
        ).lastrowid

    nota = _asentar_nota(nueva_id, cfg, firma_sola)
    if firma_sola:
        cuanto = f" Tu calificación es {_num_nota(nota)}." if nota is not None else ""
        return redirect(f"/entrega/{nueva_id}", msg=(
            "Listo: esta es tu entrega definitiva." + cuanto
            + " Esta instancia no lleva revisión docente."))
    return redirect(f"/entrega/{nueva_id}", msg=(
        "Listo: presentaste esta versión como entrega definitiva, con la devolución que ya "
        "tenías. Ahora la revisa y la firma el equipo docente."))


def _num_nota(n) -> str:
    return "" if n is None else (str(int(n)) if float(n) == int(n) else f"{n:.2f}".rstrip("0").rstrip("."))


def _asentar_nota(sid: int, cfg: dict, firma_sola: bool):
    """Cierra una entrega definitiva: le calcula la calificación y, si nadie la firma, la da por firme.

    Lo comparten los dos caminos por los que nace una definitiva —marcarla desde una
    versión ya corregida, o entregarla directo cuando la instancia no admite versiones—,
    para que una entrega valga lo mismo por donde haya entrado.

    Se hace fuera del bloque de base de quien llama: en el examen escrito puntuar implica
    una llamada al modelo, y no conviene tener la conexión tomada mientras tanto. Si esa
    llamada falla, la entrega queda igual: sin nota calculada, pero registrada y con su
    devolución. Perder la entrega por no poder ponerle un número sería mucho peor.
    """
    with get_db() as db:
        sub = db.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    try:
        nota, detalle = _calificar(cfg, sub)
    except LLMError as exc:
        log.warning("no se pudo calificar la entrega %s: %s", sid, exc)
        nota, detalle = None, None
    with get_db() as db:
        db.execute("UPDATE submissions SET nota = ?, detalle_nota = ? WHERE id = ?",
                   (nota, json.dumps(detalle, ensure_ascii=False) if detalle else "", sid))
        if firma_sola:
            db.execute("UPDATE submissions SET status = 'aprobada', final_feedback_md = ai_feedback_md,"
                       " reviewed_at = ? WHERE id = ?", (utcnow(), sid))
    return nota


def _calificar(cfg: dict, sub) -> tuple:
    """La nota de una entrega y el detalle de dónde salió. (nota, detalle) o (None, None).

    En las tres modalidades la calcula el código, no el modelo: el multiple choice compara
    letras, el trabajo abierto convierte los niveles de la rúbrica, y el examen escrito suma
    los puntos que el modelo otorgó pregunta por pregunta. El número es reproducible y, sobre
    todo, se puede mostrar de dónde salió.
    """
    tipo = cfg.get("tipo")
    if tipo == "choice":
        # Ya se calculó al recibir la entrega; se conserva tal cual.
        return (sub["nota"] if "nota" in sub.keys() else None), None
    if tipo == "abierto":
        niveles = json.loads(sub["niveles"]) if (sub["niveles"] or "").strip() else []
        return nota_de_niveles(niveles), {"tipo": "niveles", "niveles": niveles}
    if tipo == "escrito":
        puntajes = puntuar_examen(cfg, sub["work_text"] or "")
        return nota_de_puntajes(puntajes), {"tipo": "puntajes", "puntajes": puntajes}
    return None, None

@app.post("/entrega/{sid}/valorar")
async def valorar(request: Request, sid: int):
    """Le sirvió o no le sirvió. Opcional, sin fricción y sin bloquear nada."""
    user, resp = _require(request, "student")
    if resp:
        return resp
    form = await request.form()
    try:
        valor = int(form.get("valor", "0"))
    except ValueError:
        valor = 0
    if valor not in (1, -1):
        return redirect(f"/entrega/{sid}")
    with get_db() as db:
        sub = db.execute(
            "SELECT * FROM submissions WHERE id = ? AND user_id = ?", (sid, user["id"])
        ).fetchone()
        if not sub:
            return redirect("/panel")
        if sub["valoracion"] is not None:
            # vale la primera respuesta: es la reacción a la devolución recién leída,
            # y es la que el formulario ofrece una sola vez
            return redirect(f"/entrega/{sid}")
        db.execute(
            "UPDATE submissions SET valoracion = ?, valoracion_texto = ?, valoracion_at = ? WHERE id = ?",
            (valor, (form.get("comentario") or "").strip()[:1000], utcnow(), sid),
        )
    return redirect(f"/entrega/{sid}", msg="Gracias: tu valoración nos ayuda a mejorar las devoluciones.")


@app.post("/entrega/{sid}/pregunta")
def pregunta(request: Request, sid: int, question: str = Form(...)):
    user, resp = _require(request, "student")
    if resp:
        return resp
    question = question.strip()
    if not question:
        return redirect(f"/entrega/{sid}", err="Escribí una pregunta.")
    if len(question) > 2000:
        return redirect(f"/entrega/{sid}", err="La pregunta es demasiado larga (máx. 2000 caracteres).")
    with get_db() as db:
        sub, assignment, course = _load_submission(db, sid)
        if not sub or sub["user_id"] != user["id"] or sub["kind"] != "practica":
            return redirect("/panel")
        if not inscripcion_habilitada(db, user["id"], course["id"]):
            return redirect(f"/entrega/{sid}",
                            err="Tu inscripción a esta cursada está deshabilitada.")
        cfg = assignment_cfg(db, course, assignment)
        if preguntas_usadas(db, sid) >= assignment["max_preguntas"]:
            return redirect(f"/entrega/{sid}", err="Ya usaste todas las preguntas de esta entrega.")
        history = [
            (q["question"], q["answer"])
            for q in db.execute("SELECT * FROM questions WHERE submission_id = ? ORDER BY id", (sid,))
        ]
    try:
        answer = answer_question(
            cfg, first_name(user["full_name"]), sub["work_text"], sub["ai_feedback_md"], history, question
        )
    except LLMError as exc:
        return redirect(f"/entrega/{sid}", err=f"No se pudo responder (no se consumió tu pregunta). {exc}")
    with get_db() as db:
        db.execute(
            "INSERT INTO questions (submission_id, question, answer, created_at) VALUES (?, ?, ?, ?)",
            (sid, question, answer, utcnow()),
        )
    return redirect(f"/entrega/{sid}")


# ---------------------------------------------------------------- staff: entregas

@app.get("/admin/entregas", response_class=HTMLResponse)
def admin_entregas(request: Request, curso: str | None = None):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    curso = _curso_param(curso)
    with get_db() as db:
        cursos = staff_editions(db, user)
        ids = _scope_ids(db, user)
        if curso is not None and not can_access_edition(db, user, curso):
            return redirect("/admin/entregas")
        where, params = [], []
        if ids is not None:
            cond, p = _course_cond("a.edition_id", ids)
            where.append(cond)
            params += p
        if curso is not None:
            where.append("a.edition_id = ?")
            params.append(curso)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = db.execute(
            f"SELECT s.*, u.full_name, u.login, a.name AS instancia, c.name || ' ' || {periodo_sql('ce')} AS course_name "
            "FROM submissions s JOIN users u ON u.id = s.user_id "
            "JOIN assignments a ON a.id = s.assignment_id JOIN course_editions ce ON ce.id = a.edition_id "
            "JOIN courses c ON c.id = ce.course_id"
            + where_sql +
            " ORDER BY (s.kind = 'final' AND s.status = 'pendiente') DESC, s.id DESC",
            params,
        ).fetchall()

        e_where, e_params = [], []
        if ids is not None:
            cond, p = _course_cond("e.edition_id", ids)
            e_where.append(cond)
            e_params += p
        if curso is not None:
            e_where.append("e.edition_id = ?")
            e_params.append(curso)
        e_sql = (" AND " + " AND ".join(e_where)) if e_where else ""
        stats = {
            "estudiantes": db.execute(
                "SELECT COUNT(DISTINCT e.user_id) n FROM enrollments e WHERE 1=1" + e_sql, e_params
            ).fetchone()["n"],
            "activos": db.execute(
                "SELECT COUNT(DISTINCT e.user_id) n FROM enrollments e JOIN users u ON u.id = e.user_id "
                "WHERE u.active = 1" + e_sql, e_params
            ).fetchone()["n"],
            "practicas": sum(1 for r in rows if r["kind"] == "practica" and r["status"] == "ok"),
            "pendientes": sum(1 for r in rows if r["kind"] == "final" and r["status"] == "pendiente"),
        }
    with get_db() as db:
        aviso = _consejo(db, user, "entregas")
    return render(request, "admin_entregas.html", rows=rows, stats=stats, cursos=cursos,
                  curso_f=curso, aviso=aviso)


@app.get("/admin/final/{sid}", response_class=HTMLResponse)
def admin_final(request: Request, sid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        sub, assignment, course = _load_submission(db, sid)
        if not sub or sub["kind"] != "final" or not can_access_edition(db, user, course["id"]):
            return redirect("/admin/entregas")
        owner = db.execute("SELECT * FROM users WHERE id = ?", (sub["user_id"],)).fetchone()
        incidentes = _incidentes(db, sub)
    return render(
        request, "admin_final.html", sub=sub, owner=owner, course=course,
        assignment=assignment, repo_leido=_repo_leido(sub), niveles=_niveles(sub),
        detalle_nota=_detalle_nota(sub), incidentes=incidentes,
        smtp_ok=smtp_configured(),
    )


@app.post("/admin/final/{sid}")
async def admin_final_post(request: Request, sid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    form = await request.form()
    action = form.get("action", "")
    feedback = form.get("feedback", "")
    motivo_reabrir = (form.get("motivo") or "").strip()
    nota = None
    with get_db() as db:
        sub, assignment, course = _load_submission(db, sid)
        if not sub or sub["kind"] != "final" or not can_access_edition(db, user, course["id"]):
            return redirect("/admin/entregas")
        owner = db.execute("SELECT * FROM users WHERE id = ?", (sub["user_id"],)).fetchone()
        if action == "reabrir":
            db.execute(
                "UPDATE submissions SET status = 'reabierta', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (user["id"], utcnow(), sid),
            )
        if action == "aprobar":
            if not feedback.strip():
                return redirect(f"/admin/final/{sid}", err="La devolución no puede quedar vacía.")
            crudo = (form.get("nota") or "").strip().replace(",", ".")
            if crudo:
                try:
                    nota = float(crudo)
                except ValueError:
                    return redirect(f"/admin/final/{sid}", err="La nota tiene que ser un número (0 a 10).")
                if not (0 <= nota <= 10):
                    return redirect(f"/admin/final/{sid}", err="La nota va de 0 a 10.")
            # cuánto se editó la propuesta de la IA: 0 = firmada tal cual, 1 = reescrita entera.
            # Es la mcursada central del sistema y se calcula una sola vez, acá.
            propuesta = sub["ai_feedback_md"] or ""
            firmada = feedback.strip()
            ratio = None
            if propuesta:
                ratio = round(1 - difflib.SequenceMatcher(None, propuesta, firmada).ratio(), 4)
            db.execute(
                "UPDATE submissions SET status = 'aprobada', final_feedback_md = ?, nota = ?,"
                " edit_ratio = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (firmada, nota, ratio, user["id"], utcnow(), sid),
            )
    if action == "aprobar":
        ok, detail = emailer.enviar(owner["email"], emailer.devolucion_aprobada(
            first_name(owner["full_name"]), course, assignment, feedback.strip(), nota))
        campus = lti.enviar_nota_al_campus(assignment["id"], owner["id"], nota)
        return redirect("/admin/entregas",
                        msg=f"Devolución aprobada para {owner['full_name']}.{campus}",
                        correo=aviso_correo(ok, detail))
    if action == "reabrir":
        ok, detail = emailer.enviar(owner["email"], emailer.entrega_reabierta(
            first_name(owner["full_name"]), course, assignment, motivo_reabrir))
        return redirect(
            "/admin/entregas",
            msg=f"Entrega de {owner['full_name']} reabierta: puede volver a entregar.",
            correo=aviso_correo(ok, detail),
        )
    return redirect("/admin/entregas")


# ---------------------------------------------------------------- staff: cursos e instancias

@app.get("/admin/cursos", response_class=HTMLResponse)
def admin_cursos(request: Request, ver: str = ""):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        filtro = _filtro_elegido(db, user, ver)
        # Se filtra antes de contar: cada cursada cuesta tres consultas, y no tiene
        # sentido pagarlas por las que no se van a mostrar.
        por_filtro, cuantas = _reparto_por_anio(staff_editions(db, user))
        cursos = por_filtro[filtro]
        rows = []
        for c in cursos:
            docentes = edition_teachers(db, c["id"])
            n_est = db.execute(
                "SELECT COUNT(*) n FROM enrollments WHERE edition_id = ?", (c["id"],)
            ).fetchone()["n"]
            n_inst = db.execute(
                "SELECT COUNT(*) n FROM assignments WHERE edition_id = ?", (c["id"],)
            ).fetchone()["n"]
            pendientes = db.execute(
                "SELECT COUNT(*) n FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
                "WHERE a.edition_id = ? AND s.kind = 'final' AND s.status = 'pendiente'",
                (c["id"],),
            ).fetchone()["n"]
            rows.append({"c": c, "docentes": docentes, "n_est": n_est, "n_inst": n_inst, "pendientes": pendientes})
    # agrupadas por materia, conservando el orden de staff_editions
    grupos = []
    for r in rows:
        if not grupos or grupos[-1]["materia"] != r["c"]["materia"]:
            grupos.append({"materia": r["c"]["materia"], "materia_id": r["c"]["course_id"], "cursadas": []})
        grupos[-1]["cursadas"].append(r)
    with get_db() as db:
        aviso = _consejo(db, user, "cursos")
    return render(request, "admin_cursos.html", grupos=grupos, rows=rows, aviso=aviso,
                  filtro=filtro, cuantas=cuantas, anio=anio_actual())


@app.get("/admin/materias", response_class=HTMLResponse)
def admin_materias(request: Request):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    if not puede_crear_materias(user):
        return redirect("/admin/cursos", err=(
            "La coordinación no habilitó que los docentes creen materias y cursadas. "
            "Pedile a la coordinación que cree la que necesitás."))
    with get_db() as db:
        materias = []
        for m in visible_courses(db, user):
            eds = course_editions(db, m["id"])
            n_est = db.execute(
                "SELECT COUNT(DISTINCT e.user_id) n FROM enrollments e "
                "JOIN course_editions ed ON ed.id = e.edition_id WHERE ed.course_id = ?",
                (m["id"],),
            ).fetchone()["n"]
            materias.append({"m": m, "cursadas": eds, "n_est": n_est,
                             "editable": puede_editar_materia(db, user, m["id"])})
    with get_db() as db:
        aviso = _consejo(db, user, "materias")
    return render(request, "admin_materias.html", materias=materias, aviso=aviso)


@app.get("/admin/materias/nueva", response_class=HTMLResponse)
def admin_materia_nueva(request: Request):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    if not puede_crear_materias(user):
        return redirect("/admin/cursos", err=(
            "La coordinación no habilitó que los docentes creen materias y cursadas. "
            "Pedile a la coordinación que cree la que necesitás."))
    return render(request, "admin_materia_nueva.html")


@app.post("/admin/materias/crear")
async def admin_materia_crear(request: Request):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    if not puede_crear_materias(user):
        return redirect("/admin/cursos", err=(
            "La coordinación no habilitó que los docentes creen materias y cursadas. "
            "Pedile a la coordinación que cree la que necesitás."))
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return redirect("/admin/materias/nueva", err="La materia necesita un nombre.")
    with get_db() as db:
        if db.execute("SELECT 1 FROM courses WHERE name = ?", (name,)).fetchone():
            return redirect("/admin/materias/nueva", err=f"Ya existe una materia «{name}».")
        mid = db.execute(
            "INSERT INTO courses (name, active, created_at, creado_por) VALUES (?, 1, ?, ?)",
            (name, utcnow(), user["id"])
        ).lastrowid
    return redirect(f"/admin/materias/{mid}", msg="Materia creada. Ahora dale su primera cursada.")


@app.get("/admin/materias/{mid}", response_class=HTMLResponse)
def admin_materia(request: Request, mid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    if not puede_crear_materias(user):
        return redirect("/admin/cursos", err=(
            "La coordinación no habilitó que los docentes creen materias y cursadas. "
            "Pedile a la coordinación que cree la que necesitás."))
    with get_db() as db:
        materia = get_course(db, mid)
        if not materia or not puede_ver_materia(db, user, mid):
            return redirect("/admin/materias")
        editable = puede_editar_materia(db, user, mid)
        eds = []
        # Una materia puede tener cursadas de varios docentes: cada uno ve las suyas.
        for ed in course_editions(db, mid):
            if not can_access_edition(db, user, ed["id"]):
                continue
            eds.append({
                "ed": ed,
                "docentes": edition_teachers(db, ed["id"]),
                "n_est": db.execute(
                    "SELECT COUNT(*) n FROM enrollments WHERE edition_id = ?", (ed["id"],)
                ).fetchone()["n"],
                "n_inst": db.execute(
                    "SELECT COUNT(*) n FROM assignments WHERE edition_id = ?", (ed["id"],)
                ).fetchone()["n"],
            })
    return render(request, "admin_materia.html", materia=materia, cursadas=eds,
                  puede_editar=editable)


@app.post("/admin/materias/{mid}")
async def admin_materia_post(request: Request, mid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    form = await request.form()
    with get_db() as db:
        materia = get_course(db, mid)
        if not materia or not puede_ver_materia(db, user, mid):
            return redirect("/admin/materias")
        if not puede_editar_materia(db, user, mid):
            return redirect(f"/admin/materias/{mid}", err=(
                "Esta materia tiene cursadas de otros docentes: renombrarla se las "
                "cambiaría a ellos también. La edita la coordinación."))
        if form.get("action") == "eliminar":
            n = db.execute("SELECT COUNT(*) n FROM course_editions WHERE course_id = ?", (mid,)).fetchone()["n"]
            if n:
                return redirect(
                    f"/admin/materias/{mid}",
                    err=f"«{materia['name']}» tiene {n} cursada{'s' if n != 1 else ''}: no se puede "
                        "eliminar. Borrá primero sus cursadas, o si ya no se dicta destildá «En el plan».",
                )
            db.execute("DELETE FROM courses WHERE id = ?", (mid,))
            return redirect("/admin/materias", msg=f"Materia «{materia['name']}» eliminada.")
        name = (form.get("name") or "").strip()
        if not name:
            return redirect(f"/admin/materias/{mid}", err="La materia necesita un nombre.")
        if name != materia["name"] and db.execute(
            "SELECT 1 FROM courses WHERE name = ? AND id != ?", (name, mid)
        ).fetchone():
            return redirect(f"/admin/materias/{mid}", err=f"Ya existe una materia «{name}».")
        db.execute(
            "UPDATE courses SET name = ?, active = ? WHERE id = ?",
            (name, 1 if form.get("active") == "1" else 0, mid),
        )
    return redirect(f"/admin/materias/{mid}", msg="Materia actualizada.")


@app.get("/admin/cursos/nuevo", response_class=HTMLResponse)
def admin_curso_nuevo(request: Request, materia: str | None = None):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    if not puede_crear_materias(user):
        return redirect("/admin/cursos", err=(
            "La coordinación no habilitó que los docentes creen materias y cursadas. "
            "Pedile a la coordinación que cree la que necesitás."))
    with get_db() as db:
        materias = all_courses(db, only_active=True)
        duplicables = staff_editions(db, user)
        # El equipo arranca con quien está creando la cursada, que es lo que el alta hace
        # igual: si es docente queda asignado sí o sí. Coordinación puede quitarse.
        propios = [db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()]
    return render(
        request, "admin_curso_nuevo.html", propios=propios, materias=materias,
        materia_f=_curso_param(materia), duplicables=duplicables, anio=str(datetime.now(AR_TZ).year),
    )


@app.post("/admin/cursos/crear")
async def admin_cursos_crear(request: Request):
    """Crea una cursada, sobre una materia existente o sobre una nueva."""
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    if not puede_crear_materias(user):
        return redirect("/admin/cursos", err=(
            "La coordinación no habilitó que los docentes creen materias y cursadas. "
            "Pedile a la coordinación que cree la que necesitás."))
    form = await request.form()
    etiqueta = (form.get("etiqueta") or "").strip()
    materia_id = form.get("materia_id") or ""
    materia_nueva = (form.get("materia_nueva") or "").strip()
    volver = "/admin/cursos/nuevo"
    if not etiqueta:
        return redirect(volver, err="La cursada necesita una etiqueta (ej.: «2026» o «2C»).")
    anio = _leer_anio(form.get("anio"))
    if anio is None:
        return redirect(volver, err=f"El año de la cursada tiene que estar entre {ANIO_MIN} y {ANIO_MAX}.")

    with get_db() as db:
        if materia_id == "nueva" or not materia_id:
            if not materia_nueva:
                return redirect(volver, err="Elegí una materia o escribí el nombre de una nueva.")
            fila = db.execute("SELECT * FROM courses WHERE name = ?", (materia_nueva,)).fetchone()
            if fila:
                cid_materia = fila["id"]
            else:
                cid_materia = db.execute(
                    "INSERT INTO courses (name, active, created_at, creado_por) VALUES (?, 1, ?, ?)",
                    (materia_nueva, utcnow(), user["id"]),
                ).lastrowid
        else:
            cid_materia = int(materia_id)
            if not get_course(db, cid_materia):
                return redirect(volver, err="Esa materia no existe.")

        if db.execute(
            "SELECT 1 FROM course_editions WHERE course_id = ? AND anio = ? AND etiqueta = ?",
            (cid_materia, anio, etiqueta),
        ).fetchone():
            materia = get_course(db, cid_materia)
            return redirect(volver,
                            err=f"«{materia['name']}» ya tiene una cursada «{etiqueta}» en {anio}.")

        eid = db.execute(
            "INSERT INTO course_editions (course_id, anio, etiqueta, active, created_at,"
            " fecha_inicio, fecha_fin) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cid_materia, anio, etiqueta, 1 if form.get("active", "1") == "1" else 0, utcnow(),
             (form.get("fecha_inicio") or "").strip(), (form.get("fecha_fin") or "").strip()),
        ).lastrowid
        # Quien la crea queda como docente, salvo que sea coordinación eligiendo a otros.
        # Sin esto un docente crearía una cursada que después no puede ver.
        if user["role"] == "docente":
            db.execute("INSERT OR IGNORE INTO course_teachers (edition_id, user_id) VALUES (?, ?)",
                       (eid, user["id"]))
        for uid in form.getlist("docentes"):
            if db.execute("SELECT 1 FROM users WHERE id = ? AND role IN ('docente', 'admin')", (int(uid),)).fetchone():
                # OR IGNORE porque quien crea ya quedó asignado arriba y además viene en la
                # lista: el selector lo trae precargado, así que el choque es lo normal.
                db.execute("INSERT OR IGNORE INTO course_teachers (edition_id, user_id) VALUES (?, ?)",
                           (eid, int(uid)))
    return redirect(f"/admin/cursos/{eid}", msg="Cursada creada — creá sus instancias de evaluación.")


@app.get("/admin/cursos/{cid}", response_class=HTMLResponse)
def admin_curso(request: Request, cid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        course = get_edition(db, cid)
        if not course or not can_access_edition(db, user, cid):
            return redirect("/admin/cursos")
        asignados = edition_teachers(db, cid)
        # la coordinación también puede figurar como docente de un curso
        docentes = db.execute(
            "SELECT * FROM users WHERE role IN ('docente', 'admin') ORDER BY role = 'admin' DESC, full_name"
        ).fetchall()
        instancias = db.execute(
            "SELECT a.*,"
            " (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id) AS n_entregas,"
            " (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id"
            "  AND s.kind = 'final' AND s.status = 'pendiente') AS pendientes"
            " FROM assignments a WHERE a.edition_id = ? ORDER BY a.id",
            (cid,),
        ).fetchall()
        inscriptos = db.execute(
            "SELECT u.*,"
            " (SELECT COUNT(*) FROM submissions s JOIN assignments a ON a.id = s.assignment_id"
            "  WHERE s.user_id = u.id AND a.edition_id = ?) AS n_entregas"
            " FROM users u JOIN enrollments e ON e.user_id = u.id"
            " WHERE e.edition_id = ? ORDER BY u.full_name",
            (cid, cid),
        ).fetchall()
    asignados_ids = {d["id"] for d in asignados}
    with get_db() as db:
        pasos = circ.circuito(db, course)
    return render(
        request, "admin_curso.html", course=course, asignados=asignados, asignados_ids=asignados_ids,
        docentes=docentes, instancias=instancias, inscriptos=inscriptos,
        vinculo_campus=lti.habilitado() and bool(lti_storage.servicios_de_cursada(cid)),
        pasos=pasos, avance=circ.resumen(pasos),
    )


@app.post("/admin/cursos/{cid}")
async def admin_curso_post(request: Request, cid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    form = await request.form()
    with get_db() as db:
        course = get_edition(db, cid)
        if not course or not can_access_edition(db, user, cid):
            return redirect("/admin/cursos")
        if form.get("action") == "eliminar":
            if user["role"] != "admin":
                return redirect(f"/admin/cursos/{cid}")
            n = db.execute(
                "SELECT COUNT(*) n FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
                "WHERE a.edition_id = ?", (cid,)
            ).fetchone()["n"]
            if n:
                return redirect(
                    f"/admin/cursos/{cid}",
                    err=f"La cursada tiene {n} entrega{'s' if n != 1 else ''}: eliminarla borraría ese historial. "
                        "Cerrala en su lugar (destildá «Cursada abierta»).",
                )
            db.execute("DELETE FROM course_editions WHERE id = ?", (cid,))
            return redirect("/admin/cursos", msg=f"Cursada «{course['nombre']}» eliminada.")
        # Etiqueta, fechas, estado y equipo son atributos de la cursada, y quien la tiene
        # asignada los administra. Eliminarla no: eso queda arriba, y ya salió por su rama.
        if True:
            etiqueta = (form.get("etiqueta") or "").strip() or course["etiqueta"]
            anio = _leer_anio(form.get("anio"), course["anio"])
            if anio is None:
                return redirect(f"/admin/cursos/{cid}",
                                err=f"El año de la cursada tiene que estar entre {ANIO_MIN} y {ANIO_MAX}.")
            # Se miran juntos: lo que no se puede repetir es materia + año + etiqueta.
            if (anio, etiqueta) != (course["anio"], course["etiqueta"]) and db.execute(
                "SELECT 1 FROM course_editions WHERE course_id = ? AND anio = ? AND etiqueta = ?"
                " AND id != ?", (course["course_id"], anio, etiqueta, cid),
            ).fetchone():
                return redirect(
                    f"/admin/cursos/{cid}",
                    err=f"«{course['materia']}» ya tiene una cursada «{etiqueta}» en {anio}.",
                )
            db.execute(
                "UPDATE course_editions SET anio = ?, etiqueta = ?, active = ?, fecha_inicio = ?,"
                " fecha_fin = ? WHERE id = ?",
                (anio, etiqueta, 1 if form.get("active") == "1" else 0,
                 (form.get("fecha_inicio") or "").strip(), (form.get("fecha_fin") or "").strip(), cid),
            )
            elegidos = {int(x) for x in form.getlist("docentes")}
            # Un docente no puede sacarse a sí mismo: la lista se reescribe entera, y el
            # descuido de destildarse lo dejaría afuera de su propia cursada sin poder volver.
            if user["role"] != "admin":
                elegidos.add(user["id"])
            validos = [uid for uid in elegidos if db.execute(
                "SELECT 1 FROM users WHERE id = ? AND role IN ('docente', 'admin')", (uid,)
            ).fetchone()]
            # Sin docentes nadie puede firmar una corrección, así que la cursada no se
            # queda vacía: si el formulario no trajo ninguno válido, se deja como estaba.
            if validos:
                db.execute("DELETE FROM course_teachers WHERE edition_id = ?", (cid,))
                for uid in validos:
                    db.execute("INSERT INTO course_teachers (edition_id, user_id) VALUES (?, ?)", (cid, uid))
    return redirect(f"/admin/cursos/{cid}", msg="Curso guardado.")


@app.get("/admin/cursos/{cid}/instancias/nueva", response_class=HTMLResponse)
def admin_instancia_nueva(request: Request, cid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        course = get_edition(db, cid)
        if not course or not can_access_edition(db, user, cid):
            return redirect("/admin/cursos")
    return render(request, "admin_instancia_identidad.html", course=course, assignment=None)


@app.post("/admin/instancias/{aid}/importar")
async def admin_instancia_importar(request: Request, aid: int, archivo: UploadFile | None = File(None)):
    """Lee el examen de un archivo y propone las duplas pregunta / respuesta para que el docente valide."""
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    volver = f"/admin/instancias/{aid}"
    with get_db() as db:
        assignment = get_assignment(db, aid)
        if not assignment:
            return redirect("/admin/cursos")
        course = get_edition(db, assignment["edition_id"])
        if not can_access_edition(db, user, course["id"]):
            return redirect("/admin/cursos")
    if not archivo or not archivo.filename:
        return redirect(volver, err="Elegí el archivo del examen para importar.")
    try:
        texto, _ = extract_text(archivo.filename, await archivo.read())
        items = split_items(texto, assignment["tipo"])
    except (ExtractionError, LLMError) as exc:
        return redirect(volver, err=f"No se pudieron importar las preguntas. {exc}")

    with get_db() as db:
        db.execute("DELETE FROM assignment_items WHERE assignment_id = ?", (aid,))
        for orden, it in enumerate(items, 1):
            db.execute(
                "INSERT INTO assignment_items (assignment_id, orden, enunciado, respuesta, opciones, puntaje)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (aid, orden, it["enunciado"], it["respuesta"], it.get("opciones", ""), it["puntaje"]),
            )
    sin_respuesta = sum(1 for i in items if not i["respuesta"])
    msg = f"Se importaron {len(items)} preguntas de {archivo.filename}: revisalas y corregí lo que haga falta."
    if sin_respuesta:
        falta = "la opción correcta" if assignment["tipo"] == "choice" else "respuesta esperada"
        msg += f" {sin_respuesta} quedaron sin {falta} (el documento no la traía)."
    return redirect(volver, msg=msg)


@app.get("/admin/instancias/{aid}/editar", response_class=HTMLResponse)
def admin_instancia_editar(request: Request, aid: int):
    """Vuelve a la pantalla 1 (nombre y tipo) de una instancia ya creada."""
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        assignment = get_assignment(db, aid)
        if not assignment:
            return redirect("/admin/cursos")
        course = get_edition(db, assignment["edition_id"])
        if not can_access_edition(db, user, course["id"]):
            return redirect("/admin/cursos")
    return render(request, "admin_instancia_identidad.html", course=course, assignment=assignment)


@app.post("/admin/instancias/{aid}/editar")
def admin_instancia_editar_post(
    request: Request, aid: int, name: str = Form(...), tipo: str = Form(""),
    requiere_revision: str = Form(""), pide_propuesta: str = Form(""),
    pide_repo: str = Form(""), usa_vision: str = Form(""), en_plataforma: str = Form(""),
    modalidad: str = Form("digital"), fecha_apertura: str = Form(""), fecha_cierre: str = Form(""),
):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    name = name.strip()
    with get_db() as db:
        assignment = get_assignment(db, aid)
        if not assignment:
            return redirect("/admin/cursos")
        course = get_edition(db, assignment["edition_id"])
        if not can_access_edition(db, user, course["id"]):
            return redirect("/admin/cursos")
        volver = f"/admin/instancias/{aid}/editar"
        if not name:
            return redirect(volver, err="La instancia necesita un nombre.")
        if db.execute(
            "SELECT 1 FROM assignments WHERE edition_id = ? AND name = ? AND id != ?",
            (course["id"], name, aid),
        ).fetchone():
            return redirect(volver, err=f"Ya existe una instancia «{name}» en este curso.")
        # el tipo solo se cambia mientras es borrador
        if assignment["active"] or tipo not in ("abierto", "escrito", "choice"):
            tipo = assignment["tipo"]
        # La propuesta solo aplica a trabajos abiertos.
        # En el choice la casilla de revisión viaja deshabilitada y no llega marcada.
        revision = 1 if (tipo == "choice" or requiere_revision == "1") else 0
        propuesta = 1 if (tipo == "abierto" and pide_propuesta == "1") else 0
        # El repositorio acompaña a un trabajo, no a un examen escrito ni a un choice.
        repo = 1 if (tipo == "abierto" and pide_repo == "1") else 0
        vision = 1 if usa_vision == "1" else 0
        if modalidad not in ("digital", "papel", "ambos"):
            modalidad = assignment["modalidad"]
        # Si acá cambió el tipo, los cupos lo siguen: un trabajo que pasa a ser parcial
        # no se queda con las tres versiones mejorables que tenía.
        plataforma, aviso_plataforma = _en_plataforma(tipo, en_plataforma, fecha_cierre)
        db.execute(
            "UPDATE assignments SET name = ?, tipo = ?, requiere_revision = ?, pide_propuesta = ?,"
            " pide_repo = ?, usa_vision = ?, en_plataforma = ?, modalidad = ?, fecha_apertura = ?,"
            " fecha_cierre = ?, max_practicas = ?, max_preguntas = ? WHERE id = ?",
            (name, tipo, revision, propuesta, repo, vision, plataforma, modalidad,
             fecha_apertura.strip(), fecha_cierre.strip(),
             circ.ajustar(tipo, "practicas", assignment["max_practicas"]),
             circ.ajustar(tipo, "preguntas", assignment["max_preguntas"]), aid),
        )

        aviso = ""
        if tipo != assignment["tipo"]:
            guardado = []
            if tipo == "abierto" and assignment["respuestas"].strip():
                guardado.append("las respuestas / clave")
            if tipo == "choice" and assignment["rubrica"].strip():
                guardado.append("la rúbrica")
            aviso = (
                f" Quedó guardado material del tipo anterior ({' y '.join(guardado)}): no se muestra, "
                "pero reaparece si volvés a ese tipo." if guardado else ""
            )
    return redirect(f"/admin/instancias/{aid}",
                    msg="Nombre y tipo actualizados." + aviso + aviso_plataforma)


@app.post("/admin/cursos/{cid}/instancias")
def admin_instancia_crear(
    request: Request, cid: int, name: str = Form(...), tipo: str = Form("abierto"),
    requiere_revision: str = Form(""), pide_propuesta: str = Form(""),
    pide_repo: str = Form(""), usa_vision: str = Form(""), en_plataforma: str = Form(""),
    modalidad: str = Form("digital"), fecha_apertura: str = Form(""), fecha_cierre: str = Form(""),
):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    name = name.strip()
    if tipo not in ("abierto", "escrito", "choice"):
        tipo = "abierto"
    with get_db() as db:
        course = get_edition(db, cid)
        if not course or not can_access_edition(db, user, cid):
            return redirect("/admin/cursos")
        if not name:
            return redirect(f"/admin/cursos/{cid}/instancias/nueva", err="La instancia necesita un nombre (ej.: TP1, Parcial, Trabajo Final).")
        if db.execute("SELECT 1 FROM assignments WHERE edition_id = ? AND name = ?", (cid, name)).fetchone():
            return redirect(f"/admin/cursos/{cid}/instancias/nueva", err=f"Ya existe una instancia «{name}» en este curso.")
        cur = db.execute(
            "INSERT INTO assignments (edition_id, name, tipo, active, requiere_revision,"
            " pide_propuesta, pide_repo, usa_vision, en_plataforma, modalidad, fecha_apertura,"
            " fecha_cierre, max_practicas, max_preguntas, created_at)"
            " VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, name, tipo,
             # el multiple choice siempre pasa por una persona: la casilla viaja
             # deshabilitada desde el formulario y no llega marcada
             1 if (tipo == "choice" or requiere_revision == "1") else 0,
             1 if (tipo == "abierto" and pide_propuesta == "1") else 0,
             1 if (tipo == "abierto" and pide_repo == "1") else 0,
             1 if usa_vision == "1" else 0,
             _en_plataforma(tipo, en_plataforma, fecha_cierre)[0],
             modalidad if modalidad in ("digital", "papel", "ambos") else "digital",
             fecha_apertura.strip(), fecha_cierre.strip(),
             circ.defectos(tipo)["practicas"], circ.defectos(tipo)["preguntas"], utcnow()),
        )
        aid = cur.lastrowid
    return redirect(f"/admin/instancias/{aid}", msg="Instancia creada — completá el material de corrección y activala.")


@app.get("/admin/cursos/{cid}/programa", response_class=HTMLResponse)
def admin_curso_programa(request: Request, cid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        curso = get_edition(db, cid)
        if not curso or not can_access_edition(db, user, cid):
            return redirect("/admin/cursos")
    return render(request, "admin_curso_programa.html", course=curso)


@app.post("/admin/cursos/{cid}/programa")
async def admin_curso_programa_post(request: Request, cid: int):
    """El programa entra como documento; lo que se guarda es su texto, ya verificable."""
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    form = await request.form()
    volver = f"/admin/cursos/{cid}/programa"
    with get_db() as db:
        if not get_edition(db, cid) or not can_access_edition(db, user, cid):
            return redirect("/admin/cursos")

        if form.get("action") == "quitar":
            db.execute(
                "UPDATE course_editions SET programa = '', programa_archivo = '' WHERE id = ?", (cid,)
            )
            return redirect(f"/admin/cursos/{cid}", msg="Programa quitado de la cursada.")

        archivo = form.get("archivo")
        if archivo is not None and getattr(archivo, "filename", ""):
            try:
                texto, _ = extract_text(archivo.filename, await archivo.read())
            except ExtractionError as exc:
                return redirect(volver, err=str(exc))
            texto = texto.strip()
            if len(texto) < 200:
                return redirect(
                    volver,
                    err="Del archivo salieron menos de 200 caracteres. Si es un PDF escaneado no tiene "
                        "texto que extraer: subí el original en Word, o el PDF exportado desde ahí.",
                )
            db.execute(
                "UPDATE course_editions SET programa = ?, programa_archivo = ? WHERE id = ?",
                (texto, archivo.filename, cid),
            )
            return redirect(
                volver,
                msg=f"Texto extraído de «{archivo.filename}» ({len(texto)} caracteres). "
                    "Revisalo abajo: es lo que va a leer Lidia.",
            )

        # sin archivo: se está guardando la corrección del texto extraído
        texto = (form.get("programa") or "").strip()
        if not texto:
            return redirect(volver, err="Subí el programa como archivo, o dejá el texto que ya estaba.")
        db.execute("UPDATE course_editions SET programa = ? WHERE id = ?", (texto, cid))
    return redirect(f"/admin/cursos/{cid}", msg="Programa guardado.")


@app.get("/admin/cursos/{cid}/duplicar", response_class=HTMLResponse)
def admin_edicion_duplicar(request: Request, cid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        origen = get_edition(db, cid)
        if not origen or not can_access_edition(db, user, cid):
            return redirect("/admin/cursos")
        instancias = db.execute(
            "SELECT a.*, (SELECT COUNT(*) FROM assignment_items i WHERE i.assignment_id = a.id) AS n_items"
            " FROM assignments a WHERE a.edition_id = ? ORDER BY a.id",
            (cid,),
        ).fetchall()
        docentes = db.execute(
            "SELECT * FROM users WHERE role IN ('docente', 'admin') ORDER BY role = 'admin' DESC, full_name"
        ).fetchall()
        origen_docentes = {d["id"] for d in edition_teachers(db, cid)}
    return render(
        request, "admin_edicion_duplicar.html", origen=origen, instancias=instancias,
        docentes=docentes, origen_docentes=origen_docentes,
        anio_sug=origen["anio"] + 1,
        # Mientras la etiqueta sea el año, la sugerencia es el año siguiente; si dice otra
        # cosa («1C», «Verano»), se repite tal cual, que es lo que se quiere duplicar.
        etiqueta_sug=(str(origen["anio"] + 1)
                      if (origen["etiqueta"] or "").strip() == str(origen["anio"])
                      else origen["etiqueta"]),
    )


@app.post("/admin/cursos/{cid}/duplicar")
async def admin_edicion_duplicar_post(request: Request, cid: int):
    """Copia el armado de una cursada a una cursada nueva: instancias con su material,
    sus preguntas y sus cupos, más el equipo docente. Nunca estudiantes ni entregas."""
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    form = await request.form()
    etiqueta = (form.get("etiqueta") or "").strip()
    with get_db() as db:
        origen = get_edition(db, cid)
        if not origen or not can_access_edition(db, user, cid):
            return redirect("/admin/cursos")
        if not etiqueta:
            return redirect(f"/admin/cursos/{cid}/duplicar", err="La cursada nueva necesita una etiqueta.")
        anio = _leer_anio(form.get("anio"), origen["anio"] + 1)
        if anio is None:
            return redirect(f"/admin/cursos/{cid}/duplicar",
                            err=f"El año de la cursada tiene que estar entre {ANIO_MIN} y {ANIO_MAX}.")
        if db.execute(
            "SELECT 1 FROM course_editions WHERE course_id = ? AND anio = ? AND etiqueta = ?",
            (origen["course_id"], anio, etiqueta),
        ).fetchone():
            return redirect(
                f"/admin/cursos/{cid}/duplicar",
                err=f"«{origen['materia']}» ya tiene una cursada «{etiqueta}» en {anio}.",
            )

        nueva = db.execute(
            "INSERT INTO course_editions (course_id, anio, etiqueta, active, created_at)"
            " VALUES (?, ?, ?, 1, ?)",
            (origen["course_id"], anio, etiqueta, utcnow()),
        ).lastrowid

        activar = 1 if form.get("activar") == "1" else 0
        elegidas = {int(x) for x in form.getlist("instancias")}
        copiadas = 0
        for a in db.execute("SELECT * FROM assignments WHERE edition_id = ? ORDER BY id", (cid,)):
            if a["id"] not in elegidas:
                continue
            nuevo_aid = db.execute(
                "INSERT INTO assignments (edition_id, name, active, tipo, consigna, rubrica, respuestas,"
                " prompt_extra, max_practicas, max_preguntas, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (nueva, a["name"], activar, a["tipo"], a["consigna"], a["rubrica"], a["respuestas"],
                 a["prompt_extra"], a["max_practicas"], a["max_preguntas"], utcnow()),
            ).lastrowid
            for i in assignment_items(db, a["id"]):
                db.execute(
                    "INSERT INTO assignment_items (assignment_id, orden, enunciado, respuesta, opciones, puntaje)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (nuevo_aid, i["orden"], i["enunciado"], i["respuesta"], i["opciones"], i["puntaje"]),
                )
            copiadas += 1

        # Quien la crea queda como docente, salvo que sea coordinación eligiendo a otros.
        # Sin esto un docente crearía una cursada que después no puede ver.
        if user["role"] == "docente":
            db.execute("INSERT OR IGNORE INTO course_teachers (edition_id, user_id) VALUES (?, ?)",
                       (nueva, user["id"]))
        for uid in form.getlist("docentes"):
            if db.execute("SELECT 1 FROM users WHERE id = ? AND role IN ('docente', 'admin')", (int(uid),)).fetchone():
                db.execute("INSERT OR IGNORE INTO course_teachers (edition_id, user_id) VALUES (?, ?)",
                           (nueva, int(uid)))

    estado = "activas" if activar else "en borrador"
    return redirect(
        f"/admin/cursos/{nueva}",
        msg=f"Cursada «{origen['materia']} {etiqueta}» creada a partir de {origen['nombre']}: "
            f"{copiadas} instancia{'s' if copiadas != 1 else ''} copiada{'s' if copiadas != 1 else ''} {estado}. "
            "Falta cargar el listado de estudiantes.",
    )


@app.get("/admin/instancias", response_class=HTMLResponse)
def admin_instancias(request: Request):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        cursos = staff_editions(db, user)
        rows = []
        for c in cursos:
            for a in db.execute(
                "SELECT a.*,"
                " (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id) AS n_entregas,"
                " (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id"
                "  AND s.kind = 'final' AND s.status = 'pendiente') AS pendientes"
                " FROM assignments a WHERE a.edition_id = ? ORDER BY a.id",
                (c["id"],),
            ):
                rows.append({"a": a, "curso": c})
    with get_db() as db:
        aviso = _consejo(db, user, "instancias")
    return render(request, "admin_instancias.html", rows=rows, cursos=cursos, aviso=aviso)


@app.get("/admin/instancias/nueva", response_class=HTMLResponse)
def admin_instancia_nueva_global(request: Request, curso: str | None = None):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        cursos = staff_editions(db, user)
    if not cursos:
        return redirect("/admin/instancias", err="No tenés cursos asignados para crear instancias.")
    return render(
        request, "admin_instancia_identidad.html", course=None, assignment=None,
        cursos=cursos, curso_f=_curso_param(curso),
    )


@app.post("/admin/instancias/crear")
def admin_instancia_crear_global(
    request: Request, curso_id: int = Form(...), name: str = Form(...), tipo: str = Form("abierto"),
    requiere_revision: str = Form(""), pide_propuesta: str = Form(""),
    pide_repo: str = Form(""), usa_vision: str = Form(""), en_plataforma: str = Form(""),
    modalidad: str = Form("digital"), fecha_apertura: str = Form(""), fecha_cierre: str = Form(""),
):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    name = name.strip()
    if tipo not in ("abierto", "escrito", "choice"):
        tipo = "abierto"
    with get_db() as db:
        course = get_edition(db, curso_id)
        if not course or not can_access_edition(db, user, curso_id):
            return redirect("/admin/instancias", err="No podés crear instancias en ese curso.")
        if not name:
            return redirect("/admin/instancias/nueva", err="La instancia necesita un nombre (ej.: TP1, Parcial, Trabajo Final).")
        if db.execute("SELECT 1 FROM assignments WHERE edition_id = ? AND name = ?", (curso_id, name)).fetchone():
            return redirect(f"/admin/instancias/nueva?curso={curso_id}", err=f"Ya existe una instancia «{name}» en ese curso.")
        cur = db.execute(
            "INSERT INTO assignments (edition_id, name, tipo, active, requiere_revision,"
            " pide_propuesta, pide_repo, usa_vision, en_plataforma, modalidad, fecha_apertura,"
            " fecha_cierre, max_practicas, max_preguntas, created_at)"
            " VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (curso_id, name, tipo,
             # el multiple choice siempre pasa por una persona: la casilla viaja
             # deshabilitada desde el formulario y no llega marcada
             1 if (tipo == "choice" or requiere_revision == "1") else 0,
             1 if (tipo == "abierto" and pide_propuesta == "1") else 0,
             1 if (tipo == "abierto" and pide_repo == "1") else 0,
             1 if usa_vision == "1" else 0,
             _en_plataforma(tipo, en_plataforma, fecha_cierre)[0],
             modalidad if modalidad in ("digital", "papel", "ambos") else "digital",
             fecha_apertura.strip(), fecha_cierre.strip(),
             circ.defectos(tipo)["practicas"], circ.defectos(tipo)["preguntas"], utcnow()),
        )
        aid = cur.lastrowid
    return redirect(f"/admin/instancias/{aid}", msg="Instancia creada — completá el material de corrección y activala.")


@app.get("/admin/instancias/plantilla/{tipo}.{formato}")
def admin_plantilla(request: Request, tipo: str, formato: str):
    """El formulario en blanco para armar la instancia fuera del sistema."""
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    try:
        if formato == "docx":
            datos = modelos.plantilla_docx(tipo)
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif formato == "txt":
            datos = modelos.plantilla_txt(tipo).encode("utf-8")
            mime = "text/plain; charset=utf-8"
        else:
            return redirect("/admin/instancias")
    except modelos.FormatoInvalido:
        return redirect("/admin/instancias")
    nombre = f"plantilla-{modelos.NOMBRE_TIPO[tipo].replace(' ', '-')}.{formato}"
    return Response(content=datos, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


@app.post("/admin/instancias/{aid}/cargar")
async def admin_instancia_cargar(request: Request, aid: int, archivo: UploadFile = File(...)):
    """Lee el documento completado y propone los campos, sin guardar nada.

    A propósito no escribe en la base: devuelve la misma ficha con los campos llenos y
    un aviso de que están sin guardar. Así la persona ve qué se entendió antes de pisar
    lo que ya tenía, que es lo que más duele si el documento estaba mal.
    """
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    volver = f"/admin/instancias/{aid}"
    with get_db() as db:
        assignment = get_assignment(db, aid)
        if not assignment:
            return redirect("/admin/cursos")
        if not can_access_edition(db, user, assignment["edition_id"]):
            return redirect("/admin/cursos")
    # El `accept` del navegador es una sugerencia, no un control: el formato se verifica acá.
    if not (archivo.filename or "").lower().endswith(".docx"):
        return redirect(volver, err=(
            "Tiene que ser el archivo .docx de la plantilla. Un PDF no sirve: al convertirlo se "
            "pierde la estructura que se usa para separar las secciones."))
    datos = await archivo.read()
    if not datos:
        return redirect(volver, err="No elegiste ningún archivo.")
    try:
        texto, _ = extract_text(archivo.filename or "", datos)
        leido = modelos.leer(assignment["tipo"], texto)
    except (ExtractionError, modelos.FormatoInvalido) as exc:
        return redirect(volver, err=str(exc))
    return admin_instancia(request, aid, leido=leido)


@app.get("/admin/instancias/{aid}", response_class=HTMLResponse)
def admin_instancia(request: Request, aid: int, leido: dict | None = None):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        assignment = get_assignment(db, aid)
        if not assignment:
            return redirect("/admin/cursos")
        course = get_edition(db, assignment["edition_id"])
        if not can_access_edition(db, user, course["id"]):
            return redirect("/admin/cursos")
        n_entregas = db.execute(
            "SELECT COUNT(*) n FROM submissions WHERE assignment_id = ?", (aid,)
        ).fetchone()["n"]
        items = assignment_items(db, aid)
    with get_db() as db:
        pasos = circ.circuito(db, course, assignment)
    # `leido` es lo que vino de un documento recién subido: se muestra en el formulario
    # como propuesta, sin tocar la base. Nada se guarda hasta que la persona confirme.
    if leido:
        assignment = dict(assignment)
        assignment["consigna"] = leido["consigna"]
        if leido["rubrica"]:
            assignment["rubrica"] = leido["rubrica"]
        items = leido["items"] or items
    return render(
        request, "admin_instancia.html", assignment=assignment, course=course,
        n_entregas=n_entregas, items=items, puntaje_total=items_puntaje_total(items),
        cupos=circ.cupos_de(assignment),
        vinculo_campus=lti.habilitado() and bool(lti_storage.servicios_de_instancia(aid)),
        pasos=pasos, avance=circ.resumen(pasos), sin_guardar=bool(leido),
    )


@app.post("/admin/instancias/{aid}")
async def admin_instancia_post(request: Request, aid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    form = await request.form()
    action = form.get("action", "guardar")
    with get_db() as db:
        assignment = get_assignment(db, aid)
        if not assignment:
            return redirect("/admin/cursos")
        course = get_edition(db, assignment["edition_id"])
        if not can_access_edition(db, user, course["id"]):
            return redirect("/admin/cursos")
        cid = course["id"]
        if action == "eliminar":
            n = db.execute("SELECT COUNT(*) n FROM submissions WHERE assignment_id = ?", (aid,)).fetchone()["n"]
            if n:
                return redirect(
                    f"/admin/instancias/{aid}",
                    err=f"Tiene {n} entrega{'s' if n != 1 else ''}: eliminarla borraría esas devoluciones. "
                        "Si ya no se usa, destildá «Activa» y deja de verse.",
                )
            db.execute("DELETE FROM assignments WHERE id = ?", (aid,))
            return redirect(f"/admin/cursos/{cid}", msg=f"Instancia «{assignment['name']}» eliminada.")
        try:
            mp = int(form.get("max_practicas", assignment["max_practicas"]))
            mq = int(form.get("max_preguntas", assignment["max_preguntas"]))
            mi = int(form.get("max_integrantes", assignment["max_integrantes"]))
            if not (0 <= mp <= 10 and 0 <= mq <= 10 and 1 <= mi <= 8):
                raise ValueError
        except ValueError:
            return redirect(f"/admin/instancias/{aid}",
                            err="Los cupos deben ser números (versiones 0–10, preguntas 0–10, integrantes 1–8).")
        name = (form.get("name") or "").strip() or assignment["name"]
        if name != assignment["name"] and db.execute(
            "SELECT 1 FROM assignments WHERE edition_id = ? AND name = ? AND id != ?", (cid, name, aid)
        ).fetchone():
            return redirect(f"/admin/instancias/{aid}", err=f"Ya existe una instancia «{name}» en este curso.")
        tipo = form.get("tipo", assignment["tipo"])
        if tipo not in ("abierto", "escrito", "choice"):
            tipo = "abierto"
        # El tipo de evaluación manda sobre los cupos. Se ajusta acá y no solo en el
        # formulario: un campo bloqueado en la pantalla no es una restricción.
        mp = circ.ajustar(tipo, "practicas", mp)
        mq = circ.ajustar(tipo, "preguntas", mq)
        consigna = (form.get("consigna") or "").strip()
        rubrica = (form.get("rubrica") or "").strip()
        respuestas = (form.get("respuestas") or "").strip()

        # archivos opcionales: el texto extraído reemplaza al campo (queda editable)
        extraidos = []
        valores = {"consigna": consigna, "rubrica": rubrica, "respuestas": respuestas}
        try:
            for campo in ("consigna", "rubrica", "respuestas"):
                f = form.get(f"{campo}_archivo")
                if f is not None and getattr(f, "filename", ""):
                    text, _ = extract_text(f.filename, await f.read())
                    valores[campo] = text.strip()
                    extraidos.append(f.filename)
        except ExtractionError as exc:
            return redirect(f"/admin/instancias/{aid}", err=str(exc))
        consigna, rubrica, respuestas = valores["consigna"], valores["rubrica"], valores["respuestas"]

        # preguntas del examen: enunciado, respuesta esperada (u opción correcta) y puntaje
        nuevos = []
        if tipo in ("escrito", "choice"):
            enunciados = form.getlist("item_enunciado")
            respuestas_items = form.getlist("item_respuesta")
            opciones_items = form.getlist("item_opciones")
            puntajes = form.getlist("item_puntaje")
            for n, enunciado in enumerate(enunciados):
                enunciado = (enunciado or "").strip()
                if not enunciado:
                    continue
                try:
                    puntaje = float((puntajes[n] if n < len(puntajes) else "1").replace(",", ".") or 1)
                except ValueError:
                    puntaje = 1.0
                opciones = ""
                if tipo == "choice" and n < len(opciones_items):
                    opciones = "\n".join(
                        linea.strip() for linea in (opciones_items[n] or "").splitlines() if linea.strip()
                    )
                nuevos.append({
                    "enunciado": enunciado,
                    "respuesta": (respuestas_items[n] if n < len(respuestas_items) else "").strip(),
                    "opciones": opciones,
                    "puntaje": max(0.0, puntaje),
                })

        # si falta material, se guarda igual pero queda en borrador: nada de lo escrito se pierde
        active = 1 if form.get("active") == "1" else 0
        motivo = ""
        if active:
            if tipo == "escrito":
                if not nuevos:
                    motivo = "hace falta al menos una pregunta"
                elif any(not i["respuesta"] for i in nuevos):
                    motivo = "hay preguntas sin respuesta esperada"
            elif tipo == "choice":
                letras = "abcdefghij"
                if not nuevos:
                    motivo = "hace falta al menos una pregunta"
                elif any(len(i["opciones"].splitlines()) < 2 for i in nuevos):
                    motivo = "hay preguntas con menos de dos opciones"
                elif any(
                    i["respuesta"].lower() not in letras[:len(i["opciones"].splitlines())]
                    for i in nuevos
                ):
                    motivo = "hay preguntas sin una opción correcta válida"
            elif not consigna:
                motivo = "la consigna está vacía"
            elif tipo == "abierto" and not rubrica:
                motivo = "falta la rúbrica"
            if motivo:
                active = 0

        if tipo in ("escrito", "choice"):
            db.execute("DELETE FROM assignment_items WHERE assignment_id = ?", (aid,))
            for orden, i in enumerate(nuevos, 1):
                db.execute(
                    "INSERT INTO assignment_items (assignment_id, orden, enunciado, respuesta, opciones, puntaje)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (aid, orden, i["enunciado"], i["respuesta"].lower() if tipo == "choice" else i["respuesta"],
                     i["opciones"], i["puntaje"]),
                )
        db.execute(
            # `requiere_revision` y `pide_propuesta` NO se tocan acá: viven en la pantalla
            # de identidad y este formulario no los envía. Tomarlos igual de `form` los
            # ponía en cero en cada guardado, y una instancia con firma docente pasaba a
            # cerrarse sola sin que nadie lo pidiera.
            "UPDATE assignments SET name = ?, active = ?, tipo = ?, consigna = ?, rubrica = ?, respuestas = ?,"
            " prompt_extra = ?, max_practicas = ?, max_preguntas = ?, max_integrantes = ? WHERE id = ?",
            (name, active, tipo, consigna, rubrica, respuestas,
             (form.get("prompt_extra") or "").strip(), mp, mq, mi, aid),
        )
    msg = "Instancia guardada."
    if extraidos:
        msg += f" Texto extraído de {', '.join(extraidos)}: revisalo en el campo antes de activar."
    if motivo:
        return redirect(
            f"/admin/instancias/{aid}", msg=msg,
            err=f"Quedó en borrador: {motivo}. Completalo y volvé a marcar «Activa».",
        )
    return redirect(f"/admin/instancias/{aid}", msg=msg)


# ---------------------------------------------------------------- admin: docentes

@app.get("/admin/docentes/buscar")
def admin_docentes_buscar(request: Request, q: str = ""):
    """Docentes que coinciden con lo tecleado, para el selector del equipo.

    Devuelve pocos y solo a partir de tres caracteres: con cientos de docentes, una lista
    completa de casillas es inusable, y una búsqueda de una letra devuelve media facultad.
    """
    user, resp = _require(request, *STAFF)
    if resp:
        return JSONResponse([], status_code=403)
    q = q.strip()
    if len(q) < 3:
        return JSONResponse([])
    like = f"%{q}%"
    with get_db() as db:
        filas = db.execute(
            "SELECT id, full_name, login, role, active FROM users"
            " WHERE role IN ('docente', 'admin') AND (full_name LIKE ? OR login LIKE ?)"
            " ORDER BY active DESC, full_name LIMIT 8", (like, like),
        ).fetchall()
    return JSONResponse([
        {"id": f["id"], "nombre": f["full_name"], "login": f["login"],
         "coord": f["role"] == "admin", "activo": bool(f["active"])} for f in filas
    ])


@app.get("/admin/docentes", response_class=HTMLResponse)
def admin_docentes(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        rows = db.execute(
            f"SELECT u.*, (SELECT GROUP_CONCAT(c.name || ' ' || {periodo_sql('ce')}, ' · ') FROM course_teachers ct "
            " JOIN course_editions ce ON ce.id = ct.edition_id"
            " JOIN courses c ON c.id = ce.course_id"
            " WHERE ct.user_id = u.id) AS cursos "
            "FROM users u WHERE u.role IN ('docente', 'admin') ORDER BY u.role = 'admin' DESC, u.full_name"
        ).fetchall()
        cursos = staff_editions(db, user)
    return render(request, "admin_docentes.html", rows=rows, cursos=cursos)


@app.get("/admin/docentes/nuevo", response_class=HTMLResponse)
def admin_docente_nuevo(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        cursos = staff_editions(db, user)
    return render(request, "admin_docente_nuevo.html", cursos=cursos)


@app.post("/admin/docentes/crear")
async def admin_docentes_crear(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    form = await request.form()
    login_id = (form.get("login") or "").strip()
    nombre = (form.get("full_name") or "").strip()
    email = (form.get("email") or "").strip()
    if not nombre:
        return redirect("/admin/docentes/nuevo", err="El docente necesita apellido y nombre.")
    if not LOGIN_RE.match(login_id):
        return redirect("/admin/docentes/nuevo", err="Usuario inválido: 3–32 caracteres, letras/números/. _ - (sin espacios).")
    with get_db() as db:
        if db.execute("SELECT 1 FROM users WHERE login = ?", (login_id,)).fetchone():
            return redirect("/admin/docentes/nuevo", err=f"Ya existe un usuario «{login_id}».")
        cur = db.execute(
            "INSERT INTO users (login, password_hash, full_name, email, role, active, created_at)"
            " VALUES (?, ?, ?, ?, 'docente', 1, ?)",
            (login_id, claves.clave_inutilizable(), nombre, email, utcnow()),
        )
        uid = cur.lastrowid
        for cid in form.getlist("cursos"):
            db.execute("INSERT INTO course_teachers (edition_id, user_id) VALUES (?, ?)", (int(cid), uid))
    return redirect("/admin/docentes", msg=(
        f"Docente {nombre} creado → usuario: {login_id}. Todavía no tiene contraseña: "
        "mandale el enlace desde su ficha para que elija la suya."))


@app.get("/admin/docentes/{uid}", response_class=HTMLResponse)
def admin_docente(request: Request, uid: int):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        doc = db.execute("SELECT * FROM users WHERE id = ? AND role IN ('docente', 'admin')", (uid,)).fetchone()
        if not doc:
            return redirect("/admin/docentes")
        cursos = staff_editions(db, user)
        propios = {r["edition_id"] for r in db.execute(
            "SELECT edition_id FROM course_teachers WHERE user_id = ?", (uid,)
        )}
    return render(request, "admin_docente.html", doc=doc, cursos=cursos, propios=propios)


@app.post("/admin/docentes/{uid}")
async def admin_docente_post(request: Request, uid: int):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    form = await request.form()
    action = form.get("action", "")
    with get_db() as db:
        doc = db.execute("SELECT * FROM users WHERE id = ? AND role IN ('docente', 'admin')", (uid,)).fetchone()
        if not doc:
            return redirect("/admin/docentes")
        if action == "toggle" and doc["role"] == "admin":
            return redirect(f"/admin/docentes/{uid}", err="La coordinación no puede deshabilitarse desde acá.")
        if action == "eliminar":
            if doc["role"] == "admin":
                return redirect(f"/admin/docentes/{uid}", err="La coordinación no puede eliminarse desde acá.")
            n = db.execute(
                "SELECT COUNT(*) n FROM submissions WHERE reviewed_by = ?", (uid,)
            ).fetchone()["n"]
            if n:
                return redirect(
                    f"/admin/docentes/{uid}",
                    err=f"Firmó {n} corrección{'es' if n != 1 else ''} final{'es' if n != 1 else ''}: "
                        "su firma es parte del historial y no se puede eliminar. Deshabilitalo.",
                )
            db.execute("DELETE FROM users WHERE id = ?", (uid,))
            return redirect("/admin/docentes", msg=f"Docente {doc['full_name']} eliminado.")
        if action == "guardar":
            nombre = (form.get("full_name") or "").strip() or doc["full_name"]
            db.execute(
                "UPDATE users SET full_name = ?, email = ? WHERE id = ?",
                (nombre, (form.get("email") or "").strip(), uid),
            )
            db.execute("DELETE FROM course_teachers WHERE user_id = ?", (uid,))
            for cid in form.getlist("cursos"):
                db.execute("INSERT INTO course_teachers (edition_id, user_id) VALUES (?, ?)", (int(cid), uid))
            return redirect(f"/admin/docentes/{uid}", msg="Ficha actualizada.")
        if action == "toggle":
            db.execute("UPDATE users SET active = ? WHERE id = ?", (0 if doc["active"] else 1, uid))
            return redirect(f"/admin/docentes/{uid}", msg="Estado actualizado.")
        if action == "reset_password":
            return _mandar_enlace(doc, f"/admin/docentes/{uid}")
    return redirect(f"/admin/docentes/{uid}")


# ---------------------------------------------------------------- staff: estudiantes

def _crear_o_inscribir(db, edition_id: int, dni: str, nombre: str, email: str, profile: str = "") -> tuple[str, str]:
    """Devuelve (resultado, dato): ('creado'|'inscripto'|'ya_estaba', nombre) | ('error', motivo).

    La cuenta nace sin contraseña utilizable: su dueño elige la suya con el enlace que se
    le manda al correo. Acá no hay ninguna credencial que devolver.
    """
    if not dni.isdigit() or not (6 <= len(dni) <= 9):
        return "error", f"DNI inválido ({dni})"
    row = db.execute("SELECT * FROM users WHERE login = ?", (dni,)).fetchone()
    if row:
        if row["role"] != "student":
            return "error", f"{dni} ya existe y no es estudiante"
        if profile:
            db.execute("UPDATE users SET profile = ? WHERE id = ?", (profile, row["id"]))
        if enroll(db, row["id"], edition_id):
            return "inscripto", row["full_name"]
        return "ya_estaba", row["full_name"]
    cur = db.execute(
        "INSERT INTO users (login, password_hash, full_name, email, role, active, profile, created_at)"
        " VALUES (?, ?, ?, ?, 'student', 1, ?, ?)",
        (dni, claves.clave_inutilizable(), nombre, email, profile, utcnow()),
    )
    enroll(db, cur.lastrowid, edition_id)
    return "creado", nombre


def _alta_estudiantes(db, edition_id: int, lines: list[str]):
    """Crea/inscribe estudiantes desde líneas «DNI, Apellido y Nombre, correo»."""
    creados, inscriptos, ya_estaban, errores = [], [], [], []
    for i, line in enumerate(lines, 1):
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        for sep in (";", "\t"):
            line = line.replace(sep, ",")
        parts = [p.strip() for p in line.split(",") if p.strip()]
        if len(parts) < 2:
            errores.append(f"línea {i}: se esperaba «DNI, Apellido y Nombre, correo»")
            continue
        dni, nombre = parts[0], parts[1]
        email = parts[2] if len(parts) > 2 else ""
        res, dato = _crear_o_inscribir(db, edition_id, dni, nombre, email)
        if res == "creado":
            creados.append(dato)
        elif res == "inscripto":
            inscriptos.append(dato)
        elif res == "ya_estaba":
            ya_estaban.append(dato)
        else:
            errores.append(f"línea {i}: {dato}")
    return creados, inscriptos, ya_estaban, errores


@app.get("/admin/estudiantes", response_class=HTMLResponse)
def admin_estudiantes(request: Request, curso: str | None = None):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    curso = _curso_param(curso)
    with get_db() as db:
        cursos = staff_editions(db, user)
        ids = _scope_ids(db, user)
        if curso is not None and not can_access_edition(db, user, curso):
            return redirect("/admin/estudiantes")
        if curso is not None:
            students = db.execute(
                "SELECT DISTINCT u.* FROM users u JOIN enrollments e ON e.user_id = u.id "
                "WHERE u.role = 'student' AND e.edition_id = ? ORDER BY u.full_name", (curso,)
            ).fetchall()
        elif ids is None:
            students = db.execute(
                "SELECT * FROM users WHERE role = 'student' ORDER BY full_name"
            ).fetchall()
        elif ids:
            marks = ",".join("?" * len(ids))
            students = db.execute(
                f"SELECT DISTINCT u.* FROM users u JOIN enrollments e ON e.user_id = u.id "
                f"WHERE u.role = 'student' AND e.edition_id IN ({marks}) ORDER BY u.full_name", ids
            ).fetchall()
        else:
            students = []
        detalle = {}
        for s in students:
            detalle[s["id"]] = db.execute(
                "SELECT c.id AS cid, c.name,"
                " (SELECT COUNT(*) FROM submissions x JOIN assignments a ON a.id = x.assignment_id"
                "  WHERE x.user_id = e.user_id AND a.edition_id = ed.id) AS n_entregas"
                " FROM enrollments e JOIN course_editions ed ON ed.id = e.edition_id"
                " JOIN courses c ON c.id = ed.course_id"
                " WHERE e.user_id = ? ORDER BY c.name",
                (s["id"],),
            ).fetchall()
    with get_db() as db:
        aviso = _consejo(db, user, "estudiantes")
    return render(
        request, "admin_estudiantes.html", rows=students, detalle=detalle,
        cursos=cursos, curso_f=curso, aviso=aviso,
    )


@app.get("/admin/estudiantes/nuevo", response_class=HTMLResponse)
def admin_estudiante_nuevo(request: Request, curso: str | None = None):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        cursos = staff_editions(db, user)
    if not cursos:
        return redirect("/admin/estudiantes", err="No tenés cursos asignados para dar altas.")
    return render(request, "admin_estudiante_nuevo.html", cursos=cursos, curso_f=_curso_param(curso))


@app.get("/admin/estudiantes/importar", response_class=HTMLResponse)
def admin_estudiantes_importar(request: Request, curso: str | None = None):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        cursos = staff_editions(db, user)
    if not cursos:
        return redirect("/admin/estudiantes", err="No tenés cursos asignados para importar estudiantes.")
    return render(request, "admin_estudiantes_importar.html", cursos=cursos, curso_f=_curso_param(curso))


@app.post("/admin/estudiantes/alta")
def admin_alta(
    request: Request, dni: str = Form(...), full_name: str = Form(...),
    email: str = Form(""), curso_id: int = Form(...), profile: str = Form(""),
):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    dni, full_name = dni.strip(), full_name.strip()
    if not full_name:
        return redirect(f"/admin/estudiantes/nuevo?curso={curso_id}", err="Falta el apellido y nombre.")
    with get_db() as db:
        if not can_access_edition(db, user, curso_id) or not get_edition(db, curso_id):
            return redirect("/admin/estudiantes", err="No podés dar altas en ese curso.")
        res, dato = _crear_o_inscribir(db, curso_id, dni, full_name, email.strip(), profile.strip())
    if res == "error":
        return redirect(f"/admin/estudiantes/nuevo?curso={curso_id}", err=dato)
    if res == "creado":
        return redirect(
            f"/admin/estudiantes?curso={curso_id}",
            msg=(f"{full_name} dado/a de alta → usuario: {dni}. Todavía no tiene contraseña: "
                 "mandale el enlace desde su ficha para que elija la suya."),
        )
    if res == "inscripto":
        return redirect(f"/admin/estudiantes?curso={curso_id}", msg=f"{dato} ya existía: quedó inscripto/a en el curso.")
    return redirect(f"/admin/estudiantes?curso={curso_id}", msg=f"{dato} ya estaba inscripto/a en el curso.")


@app.post("/admin/estudiantes/cargar")
async def admin_cargar(
    request: Request, curso_id: int = Form(...),
    listado: str = Form(""), archivo: UploadFile | None = File(None),
):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    lines = []
    if archivo and archivo.filename:
        data = await archivo.read()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        lines += text.splitlines()
    if listado.strip():
        lines += listado.strip().splitlines()
    if not lines:
        return redirect(f"/admin/estudiantes/importar?curso={curso_id}", err="Subí un CSV o pegá el listado.")
    with get_db() as db:
        if not can_access_edition(db, user, curso_id) or not get_edition(db, curso_id):
            return redirect("/admin/estudiantes", err="No podés dar altas en ese curso.")
        creados, inscriptos, ya_estaban, errores = _alta_estudiantes(db, curso_id, lines)
    msg = f"Se crearon {len(creados)} estudiantes."
    if inscriptos:
        msg += f" Ya existían y se inscribieron: {len(inscriptos)}."
    if ya_estaban:
        msg += f" Ya estaban en el curso: {len(ya_estaban)}."
    err = " · ".join(errores)
    return redirect(f"/admin/estudiantes?curso={curso_id}", msg=msg, err=err)


@app.post("/admin/estudiantes/{uid}/toggle")
def admin_toggle(request: Request, uid: int, curso_id: int = Form(...),
                 volver: str = Form("")):
    """Habilita o deshabilita a alguien EN UNA CURSADA.

    Antes esto apagaba la cuenta entera, así que un docente de una cursada dejaba a la
    persona sin poder entregar en las cursadas de todos los demás. Se decide por cursada
    porque es quien dicta esa cursada el que tiene el criterio para decidirlo.
    """
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    destino = volver if volver.startswith("/admin/") else "/admin/estudiantes"
    with get_db() as db:
        if not can_access_edition(db, user, curso_id):
            return redirect(destino)
        fila = db.execute(
            "SELECT * FROM enrollments WHERE user_id = ? AND edition_id = ?", (uid, curso_id)
        ).fetchone()
        if not fila:
            return redirect(destino, err="Esa persona no está inscripta en esa cursada.")
        nuevo = 0 if fila["active"] else 1
        db.execute("UPDATE enrollments SET active = ? WHERE id = ?", (nuevo, fila["id"]))
        curso = get_edition(db, curso_id)
    estado = "habilitada" if nuevo else "deshabilitada"
    return redirect(destino, msg=f"Inscripción {estado} en «{curso['nombre']}». "
                                 "Solo afecta a esta cursada.")


@app.get("/admin/estudiantes/{uid}", response_class=HTMLResponse)
def admin_ficha(request: Request, uid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE id = ? AND role = 'student'", (uid,)).fetchone()
        if not row or not _student_in_scope(db, user, uid):
            return redirect("/admin/estudiantes")
        entregas = db.execute(
            f"SELECT s.*, a.name AS instancia, c.name || ' ' || {periodo_sql('ce')} AS course_name FROM submissions s "
            "JOIN assignments a ON a.id = s.assignment_id JOIN course_editions ce ON ce.id = a.edition_id "
            "JOIN courses c ON c.id = ce.course_id "
            "WHERE s.user_id = ? ORDER BY s.id DESC", (uid,)
        ).fetchall()
        # Se listan las CURSADAS, no las materias: el enlace de cada fila va a la ficha de
        # una cursada, y `habilitado` es de la inscripción, que es lo que se habilita.
        inscripciones = db.execute(
            "SELECT ed.id AS id, ed.active AS active, ed.etiqueta, ed.anio,"
            f" c.name || ' ' || {periodo_sql('ed')} AS name, e.active AS habilitado, e.id AS eid,"
            " (SELECT COUNT(*) FROM submissions s JOIN assignments a ON a.id = s.assignment_id"
            "  WHERE s.user_id = ? AND a.edition_id = ed.id) AS n_entregas"
            " FROM enrollments e JOIN course_editions ed ON ed.id = e.edition_id"
            " JOIN courses c ON c.id = ed.course_id WHERE e.user_id = ? ORDER BY c.name, ed.created_at DESC",
            (uid, uid),
        ).fetchall()
        inscripto_ids = {i["id"] for i in inscripciones}
        disponibles = [c for c in staff_editions(db, user) if c["id"] not in inscripto_ids and c["active"]]
    return render(
        request, "admin_ficha.html", est=row, entregas=entregas,
        inscripciones=inscripciones, disponibles=disponibles,
    )


@app.post("/admin/estudiantes/{uid}")
def admin_ficha_post(
    request: Request, uid: int, action: str = Form(...),
    profile: str = Form(""), email: str = Form(""), full_name: str = Form(""),
    curso_id: int = Form(0),
):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE id = ? AND role = 'student'", (uid,)).fetchone()
        if not row or not _student_in_scope(db, user, uid):
            return redirect("/admin/estudiantes")
        if action == "eliminar":
            n = db.execute(
                "SELECT COUNT(*) n FROM submissions WHERE user_id = ?", (uid,)
            ).fetchone()["n"]
            if n:
                return redirect(
                    f"/admin/estudiantes/{uid}",
                    err=f"Tiene {n} entrega{'s' if n != 1 else ''}: eliminarlo borraría su historial. Deshabilitalo.",
                )
            db.execute("DELETE FROM users WHERE id = ?", (uid,))
            return redirect("/admin/estudiantes", msg=f"Estudiante {row['full_name']} eliminado.")
        if action == "guardar":
            db.execute(
                "UPDATE users SET profile = ?, email = ?, full_name = ? WHERE id = ?",
                (profile.strip(), email.strip(), full_name.strip() or row["full_name"], uid),
            )
            return redirect(f"/admin/estudiantes/{uid}", msg="Ficha actualizada.")
        if action == "reset_password":
            return _mandar_enlace(row, f"/admin/estudiantes/{uid}")
        if action == "inscribir":
            if not can_access_edition(db, user, curso_id) or not get_edition(db, curso_id):
                return redirect(f"/admin/estudiantes/{uid}", err="No podés inscribir en ese curso.")
            if enroll(db, uid, curso_id):
                return redirect(f"/admin/estudiantes/{uid}", msg="Inscripción agregada.")
            return redirect(f"/admin/estudiantes/{uid}", msg="Ya estaba inscripto/a en ese curso.")
        if action == "desinscribir":
            if not can_access_edition(db, user, curso_id):
                return redirect(f"/admin/estudiantes/{uid}", err="No podés modificar ese curso.")
            n = db.execute(
                "SELECT COUNT(*) n FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
                "WHERE s.user_id = ? AND a.edition_id = ?", (uid, curso_id)
            ).fetchone()["n"]
            if n:
                return redirect(
                    f"/admin/estudiantes/{uid}",
                    err="Tiene entregas en ese curso: no se puede desinscribir sin perder historial. "
                        "Si hace falta, deshabilitá al estudiante.",
                )
            db.execute("DELETE FROM enrollments WHERE user_id = ? AND edition_id = ?", (uid, curso_id))
            return redirect(f"/admin/estudiantes/{uid}", msg="Inscripción quitada.")
    return redirect(f"/admin/estudiantes/{uid}")


@app.get("/admin/cursos/{cid}/notas.csv")
def admin_notas_csv(request: Request, cid: int):
    """Las notas de la cursada, listas para pegar en la planilla de la universidad."""
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        ed = get_edition(db, cid)
        if not ed or not can_access_edition(db, user, cid):
            return redirect("/admin/cursos")
        filas = db.execute(
            "SELECT u.login, u.full_name, a.name AS instancia, s.nota, s.status, s.reviewed_at"
            " FROM enrollments e"
            " JOIN users u ON u.id = e.user_id"
            " JOIN assignments a ON a.edition_id = e.edition_id"
            " LEFT JOIN submissions s ON s.user_id = u.id AND s.assignment_id = a.id"
            "   AND s.kind = 'final' AND s.status = 'aprobada'"
            " WHERE e.edition_id = ? ORDER BY u.full_name, a.id",
            (cid,),
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["dni", "apellido_y_nombre", "materia", "edicion", "instancia", "nota", "corregida_el"])
    for r in filas:
        w.writerow([
            r["login"], r["full_name"], ed["materia"], ed["etiqueta"], r["instancia"],
            "" if r["nota"] is None else f"{r['nota']:g}",
            fecha(r["reviewed_at"]) if r["reviewed_at"] else "",
        ])
    buf.seek(0)
    nombre = f"notas-{ed['materia'][:30].strip().replace(' ', '-')}-{ed['etiqueta'].replace(' ', '-')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={nombre}"},
    )


# ---------------------------------------------------------------- investigación

@app.get("/admin/investigacion", response_class=HTMLResponse)
def admin_investigacion(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        datos = investigacion.resumen(db)
    return render(request, "admin_investigacion.html", r=datos, campos=investigacion.CAMPOS)


@app.get("/admin/investigacion/{archivo}.csv")
def admin_investigacion_csv(request: Request, archivo: str):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    if archivo not in ("entregas", "configuraciones"):
        return redirect("/admin/investigacion")
    with get_db() as db:
        contenido = (investigacion.csv_datos(db) if archivo == "entregas"
                     else investigacion.csv_configuraciones(db))
    return StreamingResponse(
        iter([contenido]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=lidia-{archivo}.csv"},
    )


# ------------------------------------------------- staff: examen en papel por el estudiante

def _papel_contexto(db, user, aid):
    """Valida que la instancia admita carga docente en papel.

    Devuelve (assignment, curso, salida). `salida` es None si todo está bien, o el par
    (a dónde volver, qué decir): el motivo viaja con el destino porque no es el mismo
    decirle a alguien que la instancia es solo digital que decirle que no es suya.
    """
    assignment = get_assignment(db, aid)
    if not assignment:
        return None, None, ("/admin/instancias", "Esa instancia de evaluación no existe.")
    curso = get_edition(db, assignment["edition_id"])
    if not can_access_edition(db, user, curso["id"]):
        return None, None, ("/admin/instancias", "Esa instancia es de una cursada que no tenés asignada.")
    if assignment["modalidad"] == "digital":
        return None, None, (f"/admin/instancias/{aid}", "Esta instancia no admite exámenes en papel.")
    return assignment, curso, None


@app.get("/admin/instancias/{aid}/papel", response_class=HTMLResponse)
def admin_papel(request: Request, aid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        assignment, curso, salida = _papel_contexto(db, user, aid)
        if salida:
            return redirect(salida[0], err=salida[1])
        inscriptos = db.execute(
            "SELECT u.id, u.login, u.full_name,"
            " (SELECT COUNT(*) FROM submissions s WHERE s.user_id = u.id AND s.assignment_id = ?"
            "    AND s.kind = 'final') AS tiene_final"
            " FROM enrollments e JOIN users u ON u.id = e.user_id"
            " WHERE e.edition_id = ? ORDER BY u.full_name",
            (aid, curso["id"]),
        ).fetchall()
    return render(request, "admin_papel.html", assignment=assignment, course=curso,
                  inscriptos=inscriptos)


@app.post("/admin/instancias/{aid}/papel")
async def admin_papel_leer(
    request: Request, aid: int, alumno_id: int = Form(...), fotos: list[UploadFile] = File(...),
):
    """Transcribe las fotos y muestra la lectura para que el equipo docente la corrija."""
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    volver = f"/admin/instancias/{aid}/papel"
    with get_db() as db:
        assignment, curso, salida = _papel_contexto(db, user, aid)
        if salida:
            return redirect(salida[0], err=salida[1])
        alumno = db.execute(
            "SELECT u.* FROM users u JOIN enrollments e ON e.user_id = u.id"
            " WHERE u.id = ? AND e.edition_id = ?", (alumno_id, curso["id"]),
        ).fetchone()
        if not alumno:
            return redirect(volver, err="Ese estudiante no está inscripto en la cursada.")
        if final_activa(db, alumno_id, aid):
            return redirect(
                volver,
                err=f"{alumno['full_name']} ya tiene una entrega final en curso para esta instancia.",
            )

    try:
        imagenes = await _leer_fotos(fotos)
    except ValueError as exc:
        return redirect(volver, err=str(exc))
    try:
        transcripcion = transcribe_images(imagenes)
    except LLMError as exc:
        return redirect(volver, err=f"No se pudo leer el examen. {exc}")

    return render(request, "admin_papel_confirmar.html", assignment=assignment, course=curso,
                  alumno=alumno, transcripcion=transcripcion, n_fotos=len(imagenes))


@app.post("/admin/instancias/{aid}/papel/registrar")
async def admin_papel_registrar(
    request: Request, aid: int, alumno_id: int = Form(...), texto: str = Form(...),
    fotos_n: int = Form(0),
):
    """Registra la entrega a nombre del estudiante, dejando constancia de quién la subió."""
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    volver = f"/admin/instancias/{aid}/papel"
    texto = texto.strip()
    if len(texto) < 20:
        return redirect(volver, err="La transcripción quedó vacía o demasiado corta.")
    with get_db() as db:
        assignment, curso, salida = _papel_contexto(db, user, aid)
        if salida:
            return redirect(salida[0], err=salida[1])
        alumno = db.execute(
            "SELECT u.* FROM users u JOIN enrollments e ON e.user_id = u.id"
            " WHERE u.id = ? AND e.edition_id = ?", (alumno_id, curso["id"]),
        ).fetchone()
        if not alumno:
            return redirect(volver, err="Ese estudiante no está inscripto en la cursada.")
        if final_activa(db, alumno_id, aid):
            return redirect(volver, err=f"{alumno['full_name']} ya tiene una entrega final en curso.")
        cfg = assignment_cfg(db, curso, assignment)

    tele = {}
    try:
        feedback, model, tele = generate_feedback(
            cfg, first_name(alumno["full_name"]), alumno["profile"] or "", texto, "final", False
        )
        error = ""
    except LLMError as exc:
        feedback, model, error = "", "", str(exc)

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO submissions (user_id, assignment_id, kind, status, original_filename,"
            " work_text, text_chars, truncated, ai_feedback_md, model_used, error, created_at,"
            " cfg_snapshot, tokens_in, tokens_out, latencia_ms, finish_reason, cargada_por)"
            " VALUES (?, ?, 'final', 'pendiente', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (alumno_id, aid, f"examen en papel ({fotos_n} foto{'s' if fotos_n != 1 else ''})",
             texto, len(texto), feedback, model, error, utcnow(),
             json.dumps(cfg, ensure_ascii=False), tele.get("tokens_in"), tele.get("tokens_out"),
             tele.get("latencia_ms"), tele.get("finish_reason"), user["id"]),
        )
        sid = cur.lastrowid
    # Mismo cierre que las entregas que sube el propio estudiantado: se calcula la
    # calificación, y si la instancia no lleva firma humana la entrega queda firme acá.
    # Que la haya cargado el equipo docente cambia quién la subió, no cuánto vale ni por
    # qué circuito pasa.
    firma_sola = not assignment["requiere_revision"]
    nota = _asentar_nota(sid, cfg, firma_sola)
    aviso = f" Ojo: no se pudo generar la propuesta de corrección ({error})" if error else ""
    if firma_sola:
        cuanto = f" Calificación: {_num_nota(nota)}." if nota is not None else ""
        return redirect(f"/entrega/{sid}", msg=(
            f"Examen de {alumno['full_name']} registrado y cerrado.{cuanto}"
            f" Esta instancia no lleva revisión docente.{aviso}"))
    return redirect(f"/admin/final/{sid}",
                    msg=f"Examen de {alumno['full_name']} registrado.{aviso}")


@app.get("/admin/config", response_class=HTMLResponse)
def admin_config(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        cfg = get_config(db)
    return render(request, "admin_config.html", cfg=cfg, modelo=model_info(), smtp_ok=smtp_configured())


@app.post("/admin/config")
def admin_config_post(
    request: Request,
    banner_deshabilitado: str = Form(""), enviar_nombre: str = Form("0"),
    docentes_crean_materias: str = Form("0"),
):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        set_config(db, "banner_deshabilitado", banner_deshabilitado.strip())
        set_config(db, "enviar_nombre", "1" if enviar_nombre == "1" else "0")
        set_config(db, "docentes_crean_materias",
                   "1" if docentes_crean_materias == "1" else "0")
    return redirect("/admin/config", msg="Configuración guardada.")
