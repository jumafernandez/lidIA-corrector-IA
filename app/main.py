"""LidIA — devoluciones formativas con IA. LICDIA · UNLu."""
import csv
import difflib
import io
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import markdown as md_lib
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, investigacion
from .db import (all_courses, ventana_entrega, fecha_corta, assignment_cfg, assignment_items, can_access_edition,
                 grupo_de, grupos_de_instancia, miembros_de,
                 course_editions, edition_assignments, edition_teachers, enroll, final_activa,
                 get_assignment, get_config, get_course, get_db, get_edition, init_db,
                 is_enrolled, items_puntaje_total, practicas_usadas, preguntas_usadas,
                 set_config, staff_editions, student_editions, teacher_edition_ids, utcnow)
from . import emailer
from .emailer import desvio, smtp_configured
from .extract import ExtractionError, extract_text
from .llm import (LLMError, answer_question, generate_feedback, model_info, split_items,
                  transcribe_images)

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


@app.on_event("startup")
def _startup():
    init_db()


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", auth.current_user(request))
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx.setdefault("err", request.query_params.get("err", ""))
    return templates.TemplateResponse(request, template, ctx)


def redirect(url: str, msg: str = "", err: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    if err:
        url += ("&" if "?" in url else "?") + "err=" + quote(err)
    return RedirectResponse(BASE_PATH + url, status_code=303)


def _require(request: Request, *roles: str):
    user = auth.current_user(request)
    if not user:
        return None, redirect("/login")
    if user["role"] not in roles:
        return None, redirect("/")
    return user, None


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
                       c.name AS materia, ed.etiqueta, s.error
                  FROM submissions s
                  JOIN users u ON u.id = s.user_id
                  JOIN assignments a ON a.id = s.assignment_id
                  JOIN course_editions ed ON ed.id = a.edition_id
                  JOIN courses c ON c.id = ed.course_id
                 WHERE {filtro} AND s.kind = 'final' AND s.status = 'pendiente'
                 ORDER BY s.created_at""", args).fetchall()

        porvencer = db.execute(
            f"""SELECT a.id, a.name, a.fecha_cierre, c.name AS materia, ed.etiqueta,
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
                                  if p["materia"] == c["materia"] and p["etiqueta"] == c["etiqueta"]),
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
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE login = ?", (login_id.strip(),)).fetchone()
    if not row or not auth.verify_password(password, row["password_hash"]):
        return redirect("/login", err="Usuario o contraseña incorrectos.")
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


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        auth.destroy_session(token)
    resp = redirect("/login", msg="Sesión cerrada.")
    resp.delete_cookie(auth.COOKIE_NAME, path=BASE_PATH or "/")
    return resp


@app.get("/salud")
def salud():
    return {"ok": True, "modelo": model_info()}


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
            "UPDATE users SET password_hash = ?, initial_password = NULL WHERE id = ?",
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
    if not auth.current_user(request):
        return redirect("/login")
    with get_db() as db:
        row = db.execute("SELECT avatar, avatar_mime FROM users WHERE id = ?", (uid,)).fetchone()
    if not row or not row["avatar"]:
        return Response(status_code=404)
    return Response(content=row["avatar"], media_type=row["avatar_mime"],
                    headers={"Cache-Control": "private, max-age=300"})


# ---------------------------------------------------------------- estudiante

@app.get("/panel", response_class=HTMLResponse)
def panel_root(request: Request):
    user, resp = _require(request, "student")
    if resp:
        return resp
    with get_db() as db:
        cursos = student_editions(db, user["id"])
        if len(cursos) == 1:
            return redirect(f"/panel/{cursos[0]['id']}")
        cfg = get_config(db)
        items = [{
            "c": c,
            "n_instancias": len(edition_assignments(db, c["id"], only_active=True)),
        } for c in cursos]
    return render(request, "panel_cursos.html", items=items, cfg=cfg)


@app.get("/panel/{cid}", response_class=HTMLResponse)
def panel_curso(request: Request, cid: int):
    user, resp = _require(request, "student")
    if resp:
        return resp
    with get_db() as db:
        course = get_edition(db, cid)
        if not course or not course["active"] or not is_enrolled(db, user["id"], cid):
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
        if not course["active"] or not is_enrolled(db, user["id"], course["id"]):
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
        grupo = grupo_de(db, user["id"], aid)
        companeros = [m for m in miembros_de(db, grupo["id"]) if m["id"] != user["id"]] if grupo else []
        grupo_cerrado = bool(grupo) and bool(db.execute(
            "SELECT 1 FROM submissions WHERE assignment_id = ? AND user_id IN "
            "(SELECT user_id FROM grupo_miembros WHERE grupo_id = ?)", (aid, grupo["id"])
        ).fetchone())
    maxp = assignment["max_practicas"]
    abierta, motivo_cierre = ventana_entrega(assignment)
    return render(
        request, "panel.html", cfg=cfg, course=course, assignment=assignment,
        usadas=usadas, maxp=maxp, restantes=max(0, maxp - usadas), final=final,
        entregas=entregas, items=items, puntaje_total=items_puntaje_total(items),
        grupo=grupo, companeros=companeros, grupo_cerrado=grupo_cerrado,
        ventana_abierta=abierta, motivo_cierre=motivo_cierre,
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
        if not user["active"]:
            return redirect(volver, err="Tu usuario no está habilitado.")

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
):
    user, resp = _require(request, "student")
    if resp:
        return resp
    if kind not in ("practica", "final"):
        return redirect("/panel", err="Tipo de entrega inválido.")
    back = f"/panel/instancia/{assignment_id}"
    if not user["active"]:
        return redirect(back, err="Tu usuario no está habilitado para nuevas entregas.")

    with get_db() as db:
        assignment = get_assignment(db, assignment_id)
        course = get_edition(db, assignment["edition_id"]) if assignment else None
        if (not assignment or not assignment["active"] or not course["active"]
                or not is_enrolled(db, user["id"], course["id"])):
            return redirect("/panel", err="Esa instancia de evaluación no está disponible.")
        cfg = assignment_cfg(db, course, assignment)
        if kind == "final" and not assignment["requiere_revision"]:
            return redirect(back, err="Esta instancia es solo de práctica: no tiene entrega final.")
        abierta, motivo = ventana_entrega(assignment)
        if not abierta:
            return redirect(back, err=motivo)
        if kind == "practica" and assignment["tipo"] == "choice":
            return redirect(back, err="Esta evaluación tiene una única oportunidad de entrega.")
        if kind == "practica" and practicas_usadas(db, user["id"], assignment_id) >= assignment["max_practicas"]:
            return redirect(back, err="Ya usaste todas tus devoluciones de práctica.")
        if kind == "final" and final_activa(db, user["id"], assignment_id):
            return redirect(back, err="Ya tenés una entrega final en curso.")
        if assignment["modalidad"] == "papel" and origen != "foto":
            return redirect(back, err="Esta instancia se entrega en papel: subí las fotos de tu hoja.")

    # respuestas de un multiple choice son cortas por naturaleza; una transcripción
    # de examen en papel ya pasó por la confirmación del estudiante
    min_len = 3 if assignment["tipo"] == "choice" else (50 if origen == "foto" else 200)
    filename = ""
    try:
        if archivo and archivo.filename:
            data = await archivo.read()
            filename = archivo.filename
            work_text, truncated = extract_text(filename, data)
        elif texto.strip():
            work_text, truncated = texto.strip(), False
            if origen == "foto":
                filename = f"examen en papel ({fotos_n} foto{'s' if fotos_n != 1 else ''})"
            if len(work_text) < min_len:
                return redirect(back, err="El texto pegado es demasiado corto para evaluarlo como entrega.")
        else:
            return redirect(back, err="Subí un archivo o pegá el texto de tu trabajo.")
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

    tele = {}
    try:
        feedback, model, tele = generate_feedback(
            cfg, first_name(user["full_name"]), user["profile"] or "", work_text, kind, truncated
        )
        status = "pendiente" if kind == "final" else "ok"
        error = ""
    except LLMError as exc:
        if kind == "final":
            # la final entra igual a la cola docente, sin propuesta de la IA
            feedback, model, status, error = "", "", "pendiente", str(exc)
        else:
            return redirect(back, err=f"No se pudo generar la devolución (no se consumió tu intento). {exc}")

    with get_db() as db:
        grupo = grupo_de(db, user["id"], assignment_id)
        cur = db.execute(
            "INSERT INTO submissions (user_id, assignment_id, kind, status, original_filename, work_text,"
            " text_chars, truncated, ai_feedback_md, model_used, error, created_at,"
            " grupo_id, cfg_snapshot, tokens_in, tokens_out, latencia_ms, finish_reason,"
            " propuesta_text, sin_propuesta)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], assignment_id, kind, status, filename, work_text, len(work_text), int(truncated),
             feedback, model, error, utcnow(),
             grupo["id"] if grupo else None, json.dumps(cfg, ensure_ascii=False),
             tele.get("tokens_in"), tele.get("tokens_out"), tele.get("latencia_ms"),
             tele.get("finish_reason"), propuesta_text, int(sin_propuesta)),
        )
        sid = cur.lastrowid
    if kind == "final":
        return redirect(f"/entrega/{sid}", msg="Entrega final registrada: queda en revisión del equipo docente.")
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
    if not user["active"]:
        return redirect(back, err="Tu usuario no está habilitado para nuevas entregas.")

    with get_db() as db:
        assignment = get_assignment(db, assignment_id)
        course = get_edition(db, assignment["edition_id"]) if assignment else None
        if (not assignment or not assignment["active"] or not course["active"]
                or not is_enrolled(db, user["id"], course["id"])):
            return redirect("/panel", err="Esa instancia de evaluación no está disponible.")
        if assignment["modalidad"] == "digital":
            return redirect(back, err="Esta instancia se entrega en formato digital, no en papel.")
        abierta, motivo = ventana_entrega(assignment)
        if not abierta:
            return redirect(back, err=motivo)
        if kind == "final" and not assignment["requiere_revision"]:
            return redirect(back, err="Esta instancia es solo de práctica: no tiene entrega final.")
        if kind == "practica" and assignment["tipo"] == "choice":
            return redirect(back, err="Esta evaluación tiene una única oportunidad de entrega.")
        if kind == "practica" and practicas_usadas(db, user["id"], assignment_id) >= assignment["max_practicas"]:
            return redirect(back, err="Ya usaste todas tus devoluciones de práctica.")
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
        # primera vez que el estudiante abre su devolución: dice si la leyó, y cuándo
        if user["role"] == "student" and not sub["first_viewed_at"] and sub["ai_feedback_md"]:
            db.execute("UPDATE submissions SET first_viewed_at = ? WHERE id = ?", (utcnow(), sub["id"]))
    maxq = assignment["max_preguntas"]
    puede_preguntar = (
        user["role"] == "student" and user["active"] and sub["kind"] == "practica" and len(qs) < maxq
    )
    return render(
        request, "entrega.html", sub=sub, owner=owner, course=course, assignment=assignment,
        qs=qs, maxq=maxq, q_restantes=max(0, maxq - len(qs)), puede_preguntar=puede_preguntar,
    )


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
    if not user["active"]:
        return redirect(f"/entrega/{sid}", err="Tu usuario no está habilitado para nuevas consultas.")
    with get_db() as db:
        sub, assignment, course = _load_submission(db, sid)
        if not sub or sub["user_id"] != user["id"] or sub["kind"] != "practica":
            return redirect("/panel")
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
            "SELECT s.*, u.full_name, u.login, a.name AS instancia, c.name || ' ' || ce.etiqueta AS course_name "
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
    return render(request, "admin_entregas.html", rows=rows, stats=stats, cursos=cursos, curso_f=curso)


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
    return render(
        request, "admin_final.html", sub=sub, owner=owner, course=course,
        assignment=assignment, smtp_ok=smtp_configured(),
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
            # Es la medición central del sistema y se calcula una sola vez, acá.
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
        _, detail = emailer.enviar(owner["email"], emailer.devolucion_aprobada(
            first_name(owner["full_name"]), course, assignment, feedback.strip(), nota))
        return redirect("/admin/entregas", msg=f"Devolución aprobada para {owner['full_name']}. {detail}")
    if action == "reabrir":
        _, detail = emailer.enviar(owner["email"], emailer.entrega_reabierta(
            first_name(owner["full_name"]), course, assignment, motivo_reabrir))
        return redirect(
            "/admin/entregas",
            msg=f"Entrega de {owner['full_name']} reabierta: puede volver a entregar. {detail}",
        )
    return redirect("/admin/entregas")


# ---------------------------------------------------------------- staff: cursos e instancias

@app.get("/admin/cursos", response_class=HTMLResponse)
def admin_cursos(request: Request):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        cursos = staff_editions(db, user)
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
            grupos.append({"materia": r["c"]["materia"], "materia_id": r["c"]["course_id"], "ediciones": []})
        grupos[-1]["ediciones"].append(r)
    return render(request, "admin_cursos.html", grupos=grupos, rows=rows)


@app.get("/admin/materias", response_class=HTMLResponse)
def admin_materias(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        materias = []
        for m in all_courses(db):
            eds = course_editions(db, m["id"])
            n_est = db.execute(
                "SELECT COUNT(DISTINCT e.user_id) n FROM enrollments e "
                "JOIN course_editions ed ON ed.id = e.edition_id WHERE ed.course_id = ?",
                (m["id"],),
            ).fetchone()["n"]
            materias.append({"m": m, "ediciones": eds, "n_est": n_est})
    return render(request, "admin_materias.html", materias=materias)


@app.get("/admin/materias/nueva", response_class=HTMLResponse)
def admin_materia_nueva(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    return render(request, "admin_materia_nueva.html")


@app.post("/admin/materias/crear")
async def admin_materia_crear(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return redirect("/admin/materias/nueva", err="La materia necesita un nombre.")
    with get_db() as db:
        if db.execute("SELECT 1 FROM courses WHERE name = ?", (name,)).fetchone():
            return redirect("/admin/materias/nueva", err=f"Ya existe una materia «{name}».")
        mid = db.execute(
            "INSERT INTO courses (name, active, created_at) VALUES (?, 1, ?)", (name, utcnow())
        ).lastrowid
    return redirect(f"/admin/materias/{mid}", msg="Materia creada. Ahora dale su primera cursada.")


@app.get("/admin/materias/{mid}", response_class=HTMLResponse)
def admin_materia(request: Request, mid: int):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        materia = get_course(db, mid)
        if not materia:
            return redirect("/admin/materias")
        eds = []
        for ed in course_editions(db, mid):
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
    return render(request, "admin_materia.html", materia=materia, ediciones=eds)


@app.post("/admin/materias/{mid}")
async def admin_materia_post(request: Request, mid: int):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    form = await request.form()
    with get_db() as db:
        materia = get_course(db, mid)
        if not materia:
            return redirect("/admin/materias")
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
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        docentes = db.execute(
            "SELECT * FROM users WHERE role IN ('docente', 'admin') ORDER BY role = 'admin' DESC, full_name"
        ).fetchall()
        materias = all_courses(db, only_active=True)
        duplicables = staff_editions(db, user)
    return render(
        request, "admin_curso_nuevo.html", docentes=docentes, materias=materias,
        materia_f=_curso_param(materia), duplicables=duplicables, anio=str(datetime.now(AR_TZ).year),
    )


@app.post("/admin/cursos/crear")
async def admin_cursos_crear(request: Request):
    """Crea una EDICIÓN, sobre una materia existente o sobre una nueva."""
    user, resp = _require(request, "admin")
    if resp:
        return resp
    form = await request.form()
    etiqueta = (form.get("etiqueta") or "").strip()
    materia_id = form.get("materia_id") or ""
    materia_nueva = (form.get("materia_nueva") or "").strip()
    volver = "/admin/cursos/nuevo"
    if not etiqueta:
        return redirect(volver, err="La edición necesita una etiqueta (ej.: «2026» o «2026 2C»).")

    with get_db() as db:
        if materia_id == "nueva" or not materia_id:
            if not materia_nueva:
                return redirect(volver, err="Elegí una materia o escribí el nombre de una nueva.")
            fila = db.execute("SELECT * FROM courses WHERE name = ?", (materia_nueva,)).fetchone()
            if fila:
                cid_materia = fila["id"]
            else:
                cid_materia = db.execute(
                    "INSERT INTO courses (name, active, created_at) VALUES (?, 1, ?)",
                    (materia_nueva, utcnow()),
                ).lastrowid
        else:
            cid_materia = int(materia_id)
            if not get_course(db, cid_materia):
                return redirect(volver, err="Esa materia no existe.")

        if db.execute(
            "SELECT 1 FROM course_editions WHERE course_id = ? AND etiqueta = ?", (cid_materia, etiqueta)
        ).fetchone():
            materia = get_course(db, cid_materia)
            return redirect(volver, err=f"«{materia['name']}» ya tiene una edición «{etiqueta}».")

        eid = db.execute(
            "INSERT INTO course_editions (course_id, etiqueta, active, created_at) VALUES (?, ?, 1, ?)",
            (cid_materia, etiqueta, utcnow()),
        ).lastrowid
        for uid in form.getlist("docentes"):
            if db.execute("SELECT 1 FROM users WHERE id = ? AND role IN ('docente', 'admin')", (int(uid),)).fetchone():
                db.execute("INSERT INTO course_teachers (edition_id, user_id) VALUES (?, ?)", (eid, int(uid)))
    return redirect(f"/admin/cursos/{eid}", msg="Edición creada — creá sus instancias de evaluación.")


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
    return render(
        request, "admin_curso.html", course=course, asignados=asignados, asignados_ids=asignados_ids,
        docentes=docentes, instancias=instancias, inscriptos=inscriptos,
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
                    err=f"La edición tiene {n} entrega{'s' if n != 1 else ''}: eliminarla borraría ese historial. "
                        "Cerrala en su lugar (destildá «Cursada abierta»).",
                )
            db.execute("DELETE FROM course_editions WHERE id = ?", (cid,))
            return redirect("/admin/cursos", msg=f"Edición «{course['nombre']}» eliminada.")
        if user["role"] == "admin":
            etiqueta = (form.get("etiqueta") or "").strip()
            if etiqueta and etiqueta != course["etiqueta"]:
                if db.execute(
                    "SELECT 1 FROM course_editions WHERE course_id = ? AND etiqueta = ? AND id != ?",
                    (course["course_id"], etiqueta, cid),
                ).fetchone():
                    return redirect(
                        f"/admin/cursos/{cid}",
                        err=f"«{course['materia']}» ya tiene una edición «{etiqueta}».",
                    )
                db.execute("UPDATE course_editions SET etiqueta = ? WHERE id = ?", (etiqueta, cid))
            db.execute(
                "UPDATE course_editions SET active = ?, fecha_inicio = ?, fecha_fin = ? WHERE id = ?",
                (1 if form.get("active") == "1" else 0,
                 (form.get("fecha_inicio") or "").strip(), (form.get("fecha_fin") or "").strip(), cid),
            )
            elegidos = {int(x) for x in form.getlist("docentes")}
            db.execute("DELETE FROM course_teachers WHERE edition_id = ?", (cid,))
            for uid in elegidos:
                if db.execute(
                    "SELECT 1 FROM users WHERE id = ? AND role IN ('docente', 'admin')", (uid,)
                ).fetchone():
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
        # el multiple choice se corrige como final: siempre pasa por una persona.
        # La propuesta solo aplica a trabajos abiertos.
        revision = 1 if (tipo == "choice" or requiere_revision == "1") else 0
        propuesta = 1 if (tipo == "abierto" and pide_propuesta == "1") else 0
        if modalidad not in ("digital", "papel", "ambos"):
            modalidad = assignment["modalidad"]
        db.execute(
            "UPDATE assignments SET name = ?, tipo = ?, requiere_revision = ?, pide_propuesta = ?,"
            " modalidad = ?, fecha_apertura = ?, fecha_cierre = ? WHERE id = ?",
            (name, tipo, revision, propuesta, modalidad,
             fecha_apertura.strip(), fecha_cierre.strip(), aid),
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
    return redirect(f"/admin/instancias/{aid}", msg="Nombre y tipo actualizados." + aviso)


@app.post("/admin/cursos/{cid}/instancias")
def admin_instancia_crear(
    request: Request, cid: int, name: str = Form(...), tipo: str = Form("abierto"),
    requiere_revision: str = Form(""), pide_propuesta: str = Form(""),
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
            " pide_propuesta, modalidad, fecha_apertura, fecha_cierre, created_at)"
            " VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            (cid, name, tipo,
             1 if (tipo == "choice" or requiere_revision == "1") else 0,
             1 if (tipo == "abierto" and pide_propuesta == "1") else 0,
             modalidad if modalidad in ("digital", "papel", "ambos") else "digital",
             fecha_apertura.strip(), fecha_cierre.strip(), utcnow()),
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
        etiqueta_sug=str(datetime.now(AR_TZ).year + 1),
    )


@app.post("/admin/cursos/{cid}/duplicar")
async def admin_edicion_duplicar_post(request: Request, cid: int):
    """Copia el armado de una cursada a una edición nueva: instancias con su material,
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
            return redirect(f"/admin/cursos/{cid}/duplicar", err="La edición nueva necesita una etiqueta.")
        if db.execute(
            "SELECT 1 FROM course_editions WHERE course_id = ? AND etiqueta = ?",
            (origen["course_id"], etiqueta),
        ).fetchone():
            return redirect(
                f"/admin/cursos/{cid}/duplicar",
                err=f"«{origen['materia']}» ya tiene una edición «{etiqueta}».",
            )

        nueva = db.execute(
            "INSERT INTO course_editions (course_id, etiqueta, active, created_at) VALUES (?, ?, 1, ?)",
            (origen["course_id"], etiqueta, utcnow()),
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

        for uid in form.getlist("docentes"):
            if db.execute("SELECT 1 FROM users WHERE id = ? AND role IN ('docente', 'admin')", (int(uid),)).fetchone():
                db.execute("INSERT INTO course_teachers (edition_id, user_id) VALUES (?, ?)", (nueva, int(uid)))

    estado = "activas" if activar else "en borrador"
    return redirect(
        f"/admin/cursos/{nueva}",
        msg=f"Edición «{origen['materia']} {etiqueta}» creada a partir de {origen['nombre']}: "
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
    return render(request, "admin_instancias.html", rows=rows, cursos=cursos)


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
            " pide_propuesta, modalidad, fecha_apertura, fecha_cierre, created_at)"
            " VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
            (curso_id, name, tipo,
             1 if (tipo == "choice" or requiere_revision == "1") else 0,
             1 if (tipo == "abierto" and pide_propuesta == "1") else 0,
             modalidad if modalidad in ("digital", "papel", "ambos") else "digital",
             fecha_apertura.strip(), fecha_cierre.strip(), utcnow()),
        )
        aid = cur.lastrowid
    return redirect(f"/admin/instancias/{aid}", msg="Instancia creada — completá el material de corrección y activala.")


@app.get("/admin/instancias/{aid}", response_class=HTMLResponse)
def admin_instancia(request: Request, aid: int):
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
    return render(
        request, "admin_instancia.html", assignment=assignment, course=course,
        n_entregas=n_entregas, items=items, puntaje_total=items_puntaje_total(items),
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
            if not (1 <= mp <= 10 and 0 <= mq <= 10 and 1 <= mi <= 8):
                raise ValueError
        except ValueError:
            return redirect(f"/admin/instancias/{aid}",
                            err="Los cupos deben ser números (prácticas 1–10, preguntas 0–10, integrantes 1–8).")
        name = (form.get("name") or "").strip() or assignment["name"]
        if name != assignment["name"] and db.execute(
            "SELECT 1 FROM assignments WHERE edition_id = ? AND name = ? AND id != ?", (cid, name, aid)
        ).fetchone():
            return redirect(f"/admin/instancias/{aid}", err=f"Ya existe una instancia «{name}» en este curso.")
        tipo = form.get("tipo", assignment["tipo"])
        if tipo not in ("abierto", "escrito", "choice"):
            tipo = "abierto"
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
            "UPDATE assignments SET name = ?, active = ?, tipo = ?, consigna = ?, rubrica = ?, respuestas = ?,"
            " prompt_extra = ?, max_practicas = ?, max_preguntas = ?, max_integrantes = ?,"
            " pide_propuesta = ?, requiere_revision = ? WHERE id = ?",
            (name, active, tipo, consigna, rubrica, respuestas,
             (form.get("prompt_extra") or "").strip(), mp, mq, mi,
             1 if (tipo == "abierto" and form.get("pide_propuesta") == "1") else 0,
             # el multiple choice se corrige como final: siempre pasa por una persona
             1 if (tipo == "choice" or form.get("requiere_revision") == "1") else 0, aid),
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

@app.get("/admin/docentes", response_class=HTMLResponse)
def admin_docentes(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        rows = db.execute(
            "SELECT u.*, (SELECT GROUP_CONCAT(c.name || ' ' || ce.etiqueta, ' · ') FROM course_teachers ct "
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
        password = auth.generate_password()
        cur = db.execute(
            "INSERT INTO users (login, password_hash, initial_password, full_name, email, role, active, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'docente', 1, ?)",
            (login_id, auth.hash_password(password), password, nombre, email, utcnow()),
        )
        uid = cur.lastrowid
        for cid in form.getlist("cursos"):
            db.execute("INSERT INTO course_teachers (edition_id, user_id) VALUES (?, ?)", (int(cid), uid))
    return redirect("/admin/docentes", msg=f"Docente {nombre} creado → usuario: {login_id} · contraseña: {password}")


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
            password = auth.generate_password()
            db.execute(
                "UPDATE users SET password_hash = ?, initial_password = ? WHERE id = ?",
                (auth.hash_password(password), password, uid),
            )
            return redirect(f"/admin/docentes/{uid}", msg=f"Nueva contraseña: {password}")
    return redirect(f"/admin/docentes/{uid}")


# ---------------------------------------------------------------- staff: estudiantes

def _crear_o_inscribir(db, edition_id: int, dni: str, nombre: str, email: str, profile: str = "") -> tuple[str, str]:
    """Devuelve (resultado, dato): ('creado', password) | ('inscripto'|'ya_estaba', nombre) | ('error', motivo)."""
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
    password = auth.generate_password()
    cur = db.execute(
        "INSERT INTO users (login, password_hash, initial_password, full_name, email, role, active, profile, created_at)"
        " VALUES (?, ?, ?, ?, ?, 'student', 1, ?, ?)",
        (dni, auth.hash_password(password), password, nombre, email, profile, utcnow()),
    )
    enroll(db, cur.lastrowid, edition_id)
    return "creado", password


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
            creados.append((nombre, dato))
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
    return render(
        request, "admin_estudiantes.html", rows=students, detalle=detalle,
        cursos=cursos, curso_f=curso,
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
            msg=f"{full_name} dado/a de alta → DNI: {dni} · contraseña: {dato}",
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
def admin_toggle(request: Request, uid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE id = ? AND role = 'student'", (uid,)).fetchone()
        if row and _student_in_scope(db, user, uid):
            db.execute("UPDATE users SET active = ? WHERE id = ?", (0 if row["active"] else 1, uid))
    return redirect("/admin/estudiantes")


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
            "SELECT s.*, a.name AS instancia, c.name || ' ' || ce.etiqueta AS course_name FROM submissions s "
            "JOIN assignments a ON a.id = s.assignment_id JOIN course_editions ce ON ce.id = a.edition_id "
            "JOIN courses c ON c.id = ce.course_id "
            "WHERE s.user_id = ? ORDER BY s.id DESC", (uid,)
        ).fetchall()
        inscripciones = db.execute(
            "SELECT c.*, e.id AS eid,"
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
            password = auth.generate_password()
            db.execute(
                "UPDATE users SET password_hash = ?, initial_password = ? WHERE id = ?",
                (auth.hash_password(password), password, uid),
            )
            return redirect(f"/admin/estudiantes/{uid}", msg=f"Nueva contraseña: {password}")
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
    """Las notas de la edición, listas para pegar en la planilla de la universidad."""
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
    """Valida que la instancia admita carga docente en papel y devuelve (assignment, curso)."""
    assignment = get_assignment(db, aid)
    if not assignment:
        return None, None, "/admin/instancias"
    curso = get_edition(db, assignment["edition_id"])
    if not can_access_edition(db, user, curso["id"]):
        return None, None, "/admin/instancias"
    if assignment["modalidad"] == "digital":
        return None, None, f"/admin/instancias/{aid}"
    return assignment, curso, None


@app.get("/admin/instancias/{aid}/papel", response_class=HTMLResponse)
def admin_papel(request: Request, aid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        assignment, curso, salida = _papel_contexto(db, user, aid)
        if salida:
            return redirect(salida, err="Esta instancia no admite exámenes en papel.")
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
            return redirect(salida, err="Esta instancia no admite exámenes en papel.")
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
            return redirect(salida, err="Esta instancia no admite exámenes en papel.")
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
    aviso = f" Ojo: no se pudo generar la propuesta de corrección ({error})" if error else ""
    return redirect(f"/admin/final/{sid}",
                    msg=f"Examen de {alumno['full_name']} registrado.{aviso}")


@app.get("/admin/credenciales.csv")
def admin_credenciales(request: Request, curso: str | None = None):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    curso = _curso_param(curso)
    with get_db() as db:
        ids = _scope_ids(db, user)
        if curso is not None and not can_access_edition(db, user, curso):
            return redirect("/admin/estudiantes")
        if curso is not None:
            rows = db.execute(
                "SELECT DISTINCT u.login, u.full_name, u.email, u.initial_password FROM users u "
                "JOIN enrollments e ON e.user_id = u.id WHERE u.role = 'student' AND e.edition_id = ? "
                "AND u.initial_password IS NOT NULL ORDER BY u.full_name", (curso,)
            ).fetchall()
        elif ids is None:
            rows = db.execute(
                "SELECT login, full_name, email, initial_password FROM users "
                "WHERE role = 'student' AND initial_password IS NOT NULL ORDER BY full_name"
            ).fetchall()
        elif ids:
            marks = ",".join("?" * len(ids))
            rows = db.execute(
                f"SELECT DISTINCT u.login, u.full_name, u.email, u.initial_password FROM users u "
                f"JOIN enrollments e ON e.user_id = u.id WHERE u.role = 'student' AND e.edition_id IN ({marks}) "
                f"AND u.initial_password IS NOT NULL ORDER BY u.full_name", ids
            ).fetchall()
        else:
            rows = []
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["dni", "nombre", "correo", "contraseña_inicial"])
    for r in rows:
        writer.writerow([r["login"], r["full_name"], r["email"], r["initial_password"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=credenciales.csv"},
    )


# ---------------------------------------------------------------- admin: configuración global

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
):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        set_config(db, "banner_deshabilitado", banner_deshabilitado.strip())
        set_config(db, "enviar_nombre", "1" if enviar_nombre == "1" else "0")
    return redirect("/admin/config", msg="Configuración guardada.")
