"""LidIA — devoluciones formativas con IA. LICDIA · UNLu."""
import csv
import io
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

from . import auth
from .db import (assignment_cfg, assignment_items, can_access_course, course_assignments,
                 course_teachers, enroll, final_activa, get_assignment, get_config, get_course,
                 get_db, init_db, is_enrolled, items_puntaje_total, practicas_usadas,
                 preguntas_usadas, set_config, staff_courses, student_courses,
                 teacher_course_ids, utcnow)
from .emailer import send_feedback_email, smtp_configured
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
    return teacher_course_ids(db, user)


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
        f"SELECT 1 FROM enrollments WHERE user_id = ? AND course_id IN ({marks})",
        [student_id, *ids],
    ).fetchone())


def _load_submission(db, sid: int):
    """Entrega + su instancia y curso. Devuelve (sub, assignment, course) o (None, None, None)."""
    sub = db.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    if not sub:
        return None, None, None
    assignment = get_assignment(db, sub["assignment_id"])
    course = get_course(db, assignment["course_id"])
    return sub, assignment, course


# ---------------------------------------------------------------- sesión

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = auth.current_user(request)
    if not user:
        return redirect("/login")
    return redirect("/admin/cursos" if user["role"] in STAFF else "/panel")


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
        cursos = student_courses(db, user["id"])
        if len(cursos) == 1:
            return redirect(f"/panel/{cursos[0]['id']}")
        cfg = get_config(db)
        items = [{
            "c": c,
            "n_instancias": len(course_assignments(db, c["id"], only_active=True)),
        } for c in cursos]
    return render(request, "panel_cursos.html", items=items, cfg=cfg)


@app.get("/panel/{cid}", response_class=HTMLResponse)
def panel_curso(request: Request, cid: int):
    user, resp = _require(request, "student")
    if resp:
        return resp
    with get_db() as db:
        course = get_course(db, cid)
        if not course or not course["active"] or not is_enrolled(db, user["id"], cid):
            return redirect("/panel")
        cfg = get_config(db)
        items = []
        for a in course_assignments(db, cid, only_active=True):
            usadas = practicas_usadas(db, user["id"], a["id"])
            items.append({
                "a": a,
                "restantes": max(0, a["max_practicas"] - usadas),
                "final": final_activa(db, user["id"], a["id"]),
            })
        multi = len(student_courses(db, user["id"])) > 1
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
        course = get_course(db, assignment["course_id"])
        if not course["active"] or not is_enrolled(db, user["id"], course["id"]):
            return redirect("/panel")
        cfg = get_config(db)
        usadas = practicas_usadas(db, user["id"], aid)
        final = final_activa(db, user["id"], aid)
        entregas = db.execute(
            "SELECT * FROM submissions WHERE user_id = ? AND assignment_id = ? ORDER BY id DESC",
            (user["id"], aid),
        ).fetchall()
        items = assignment_items(db, aid)
    maxp = assignment["max_practicas"]
    return render(
        request, "panel.html", cfg=cfg, course=course, assignment=assignment,
        usadas=usadas, maxp=maxp, restantes=max(0, maxp - usadas), final=final,
        entregas=entregas, items=items, puntaje_total=items_puntaje_total(items),
    )


@app.post("/entregar")
async def entregar(
    request: Request,
    kind: str = Form(...),
    assignment_id: int = Form(...),
    archivo: UploadFile | None = File(None),
    texto: str = Form(""),
    origen: str = Form(""),
    fotos_n: int = Form(0),
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
        course = get_course(db, assignment["course_id"]) if assignment else None
        if (not assignment or not assignment["active"] or not course["active"]
                or not is_enrolled(db, user["id"], course["id"])):
            return redirect("/panel", err="Esa instancia de evaluación no está disponible.")
        cfg = assignment_cfg(db, course, assignment)
        if kind == "practica" and assignment["tipo"] == "choice":
            return redirect(back, err="Esta evaluación tiene una única oportunidad de entrega.")
        if kind == "practica" and practicas_usadas(db, user["id"], assignment_id) >= assignment["max_practicas"]:
            return redirect(back, err="Ya usaste todas tus devoluciones de práctica.")
        if kind == "final" and final_activa(db, user["id"], assignment_id):
            return redirect(back, err="Ya tenés una entrega final en curso.")

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

    try:
        feedback, model = generate_feedback(
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
        cur = db.execute(
            "INSERT INTO submissions (user_id, assignment_id, kind, status, original_filename, work_text,"
            " text_chars, truncated, ai_feedback_md, model_used, error, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user["id"], assignment_id, kind, status, filename, work_text, len(work_text), int(truncated),
             feedback, model, error, utcnow()),
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
        course = get_course(db, assignment["course_id"]) if assignment else None
        if (not assignment or not assignment["active"] or not course["active"]
                or not is_enrolled(db, user["id"], course["id"])):
            return redirect("/panel", err="Esa instancia de evaluación no está disponible.")
        if assignment["tipo"] not in ("escrito", "choice"):
            return redirect(back, err="La entrega por fotos es para exámenes en papel.")
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
                return redirect("/")
        elif not can_access_course(db, user, course["id"]):
            return redirect("/")
        qs = db.execute(
            "SELECT * FROM questions WHERE submission_id = ? ORDER BY id", (sid,)
        ).fetchall()
        owner = db.execute("SELECT * FROM users WHERE id = ?", (sub["user_id"],)).fetchone()
    maxq = assignment["max_preguntas"]
    puede_preguntar = (
        user["role"] == "student" and user["active"] and sub["kind"] == "practica" and len(qs) < maxq
    )
    return render(
        request, "entrega.html", sub=sub, owner=owner, course=course, assignment=assignment,
        qs=qs, maxq=maxq, q_restantes=max(0, maxq - len(qs)), puede_preguntar=puede_preguntar,
    )


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
        cursos = staff_courses(db, user)
        ids = _scope_ids(db, user)
        if curso is not None and not can_access_course(db, user, curso):
            return redirect("/admin/entregas")
        where, params = [], []
        if ids is not None:
            cond, p = _course_cond("a.course_id", ids)
            where.append(cond)
            params += p
        if curso is not None:
            where.append("a.course_id = ?")
            params.append(curso)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = db.execute(
            "SELECT s.*, u.full_name, u.login, a.name AS instancia, c.name AS course_name "
            "FROM submissions s JOIN users u ON u.id = s.user_id "
            "JOIN assignments a ON a.id = s.assignment_id JOIN courses c ON c.id = a.course_id"
            + where_sql +
            " ORDER BY (s.kind = 'final' AND s.status = 'pendiente') DESC, s.id DESC",
            params,
        ).fetchall()

        e_where, e_params = [], []
        if ids is not None:
            cond, p = _course_cond("e.course_id", ids)
            e_where.append(cond)
            e_params += p
        if curso is not None:
            e_where.append("e.course_id = ?")
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
        if not sub or sub["kind"] != "final" or not can_access_course(db, user, course["id"]):
            return redirect("/admin/entregas")
        owner = db.execute("SELECT * FROM users WHERE id = ?", (sub["user_id"],)).fetchone()
    return render(
        request, "admin_final.html", sub=sub, owner=owner, course=course,
        assignment=assignment, smtp_ok=smtp_configured(),
    )


@app.post("/admin/final/{sid}")
def admin_final_post(request: Request, sid: int, action: str = Form(...), feedback: str = Form("")):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        sub, assignment, course = _load_submission(db, sid)
        if not sub or sub["kind"] != "final" or not can_access_course(db, user, course["id"]):
            return redirect("/admin/entregas")
        owner = db.execute("SELECT * FROM users WHERE id = ?", (sub["user_id"],)).fetchone()
        if action == "reabrir":
            db.execute(
                "UPDATE submissions SET status = 'reabierta', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (user["id"], utcnow(), sid),
            )
            return redirect("/admin/entregas", msg=f"Entrega de {owner['full_name']} reabierta: puede volver a entregar.")
        if action == "aprobar":
            if not feedback.strip():
                return redirect(f"/admin/final/{sid}", err="La devolución no puede quedar vacía.")
            db.execute(
                "UPDATE submissions SET status = 'aprobada', final_feedback_md = ?, reviewed_by = ?, reviewed_at = ? "
                "WHERE id = ?",
                (feedback.strip(), user["id"], utcnow(), sid),
            )
    if action == "aprobar":
        sent, detail = send_feedback_email(owner["email"], first_name(owner["full_name"]), feedback.strip())
        note = f"Devolución aprobada para {owner['full_name']}. {detail}"
        return redirect("/admin/entregas", msg=note)
    return redirect("/admin/entregas")


# ---------------------------------------------------------------- staff: cursos e instancias

@app.get("/admin/cursos", response_class=HTMLResponse)
def admin_cursos(request: Request):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        cursos = staff_courses(db, user)
        rows = []
        for c in cursos:
            docentes = course_teachers(db, c["id"])
            n_est = db.execute(
                "SELECT COUNT(*) n FROM enrollments WHERE course_id = ?", (c["id"],)
            ).fetchone()["n"]
            n_inst = db.execute(
                "SELECT COUNT(*) n FROM assignments WHERE course_id = ?", (c["id"],)
            ).fetchone()["n"]
            pendientes = db.execute(
                "SELECT COUNT(*) n FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
                "WHERE a.course_id = ? AND s.kind = 'final' AND s.status = 'pendiente'",
                (c["id"],),
            ).fetchone()["n"]
            rows.append({"c": c, "docentes": docentes, "n_est": n_est, "n_inst": n_inst, "pendientes": pendientes})
    return render(request, "admin_cursos.html", rows=rows)


@app.get("/admin/cursos/nuevo", response_class=HTMLResponse)
def admin_curso_nuevo(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        docentes = db.execute(
            "SELECT * FROM users WHERE role IN ('docente', 'admin') ORDER BY role = 'admin' DESC, full_name"
        ).fetchall()
    return render(request, "admin_curso_nuevo.html", docentes=docentes)


@app.post("/admin/cursos/crear")
async def admin_cursos_crear(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return redirect("/admin/cursos/nuevo", err="El curso necesita un nombre.")
    with get_db() as db:
        if db.execute("SELECT 1 FROM courses WHERE name = ?", (name,)).fetchone():
            return redirect("/admin/cursos/nuevo", err=f"Ya existe un curso llamado «{name}».")
        cur = db.execute(
            "INSERT INTO courses (name, active, created_at) VALUES (?, 1, ?)", (name, utcnow())
        )
        cid = cur.lastrowid
        for uid in form.getlist("docentes"):
            if db.execute("SELECT 1 FROM users WHERE id = ? AND role IN ('docente', 'admin')", (int(uid),)).fetchone():
                db.execute("INSERT INTO course_teachers (course_id, user_id) VALUES (?, ?)", (cid, int(uid)))
    return redirect(f"/admin/cursos/{cid}", msg="Curso creado — creá sus instancias de evaluación.")


@app.get("/admin/cursos/{cid}", response_class=HTMLResponse)
def admin_curso(request: Request, cid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        course = get_course(db, cid)
        if not course or not can_access_course(db, user, cid):
            return redirect("/admin/cursos")
        asignados = course_teachers(db, cid)
        # la coordinación también puede figurar como docente de un curso
        docentes = db.execute(
            "SELECT * FROM users WHERE role IN ('docente', 'admin') ORDER BY role = 'admin' DESC, full_name"
        ).fetchall()
        instancias = db.execute(
            "SELECT a.*,"
            " (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id) AS n_entregas,"
            " (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id"
            "  AND s.kind = 'final' AND s.status = 'pendiente') AS pendientes"
            " FROM assignments a WHERE a.course_id = ? ORDER BY a.id",
            (cid,),
        ).fetchall()
        inscriptos = db.execute(
            "SELECT u.*,"
            " (SELECT COUNT(*) FROM submissions s JOIN assignments a ON a.id = s.assignment_id"
            "  WHERE s.user_id = u.id AND a.course_id = ?) AS n_entregas"
            " FROM users u JOIN enrollments e ON e.user_id = u.id"
            " WHERE e.course_id = ? ORDER BY u.full_name",
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
        course = get_course(db, cid)
        if not course or not can_access_course(db, user, cid):
            return redirect("/admin/cursos")
        if form.get("action") == "eliminar":
            if user["role"] != "admin":
                return redirect(f"/admin/cursos/{cid}")
            n = db.execute(
                "SELECT COUNT(*) n FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
                "WHERE a.course_id = ?", (cid,)
            ).fetchone()["n"]
            if n:
                return redirect(
                    f"/admin/cursos/{cid}",
                    err=f"El curso tiene {n} entrega{'s' if n != 1 else ''}: eliminarlo borraría ese historial. "
                        "Desactivalo en su lugar.",
                )
            db.execute("DELETE FROM courses WHERE id = ?", (cid,))
            return redirect("/admin/cursos", msg=f"Curso «{course['name']}» eliminado.")
        if user["role"] == "admin":
            name = (form.get("name") or "").strip()
            if name and name != course["name"]:
                if db.execute("SELECT 1 FROM courses WHERE name = ? AND id != ?", (name, cid)).fetchone():
                    return redirect(f"/admin/cursos/{cid}", err=f"Ya existe un curso llamado «{name}».")
                db.execute("UPDATE courses SET name = ? WHERE id = ?", (name, cid))
            db.execute("UPDATE courses SET active = ? WHERE id = ?", (1 if form.get("active") == "1" else 0, cid))
            elegidos = {int(x) for x in form.getlist("docentes")}
            db.execute("DELETE FROM course_teachers WHERE course_id = ?", (cid,))
            for uid in elegidos:
                if db.execute(
                    "SELECT 1 FROM users WHERE id = ? AND role IN ('docente', 'admin')", (uid,)
                ).fetchone():
                    db.execute("INSERT INTO course_teachers (course_id, user_id) VALUES (?, ?)", (cid, uid))
    return redirect(f"/admin/cursos/{cid}", msg="Curso guardado.")


@app.get("/admin/cursos/{cid}/instancias/nueva", response_class=HTMLResponse)
def admin_instancia_nueva(request: Request, cid: int):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        course = get_course(db, cid)
        if not course or not can_access_course(db, user, cid):
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
        course = get_course(db, assignment["course_id"])
        if not can_access_course(db, user, course["id"]):
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
        course = get_course(db, assignment["course_id"])
        if not can_access_course(db, user, course["id"]):
            return redirect("/admin/cursos")
    return render(request, "admin_instancia_identidad.html", course=course, assignment=assignment)


@app.post("/admin/instancias/{aid}/editar")
def admin_instancia_editar_post(
    request: Request, aid: int, name: str = Form(...), tipo: str = Form("")
):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    name = name.strip()
    with get_db() as db:
        assignment = get_assignment(db, aid)
        if not assignment:
            return redirect("/admin/cursos")
        course = get_course(db, assignment["course_id"])
        if not can_access_course(db, user, course["id"]):
            return redirect("/admin/cursos")
        volver = f"/admin/instancias/{aid}/editar"
        if not name:
            return redirect(volver, err="La instancia necesita un nombre.")
        if db.execute(
            "SELECT 1 FROM assignments WHERE course_id = ? AND name = ? AND id != ?",
            (course["id"], name, aid),
        ).fetchone():
            return redirect(volver, err=f"Ya existe una instancia «{name}» en este curso.")
        # el tipo solo se cambia mientras es borrador
        if assignment["active"] or tipo not in ("abierto", "escrito", "choice"):
            tipo = assignment["tipo"]
        db.execute("UPDATE assignments SET name = ?, tipo = ? WHERE id = ?", (name, tipo, aid))

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
def admin_instancia_crear(request: Request, cid: int, name: str = Form(...), tipo: str = Form("abierto")):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    name = name.strip()
    if tipo not in ("abierto", "escrito", "choice"):
        tipo = "abierto"
    with get_db() as db:
        course = get_course(db, cid)
        if not course or not can_access_course(db, user, cid):
            return redirect("/admin/cursos")
        if not name:
            return redirect(f"/admin/cursos/{cid}/instancias/nueva", err="La instancia necesita un nombre (ej.: TP1, Parcial, Trabajo Final).")
        if db.execute("SELECT 1 FROM assignments WHERE course_id = ? AND name = ?", (cid, name)).fetchone():
            return redirect(f"/admin/cursos/{cid}/instancias/nueva", err=f"Ya existe una instancia «{name}» en este curso.")
        cur = db.execute(
            "INSERT INTO assignments (course_id, name, tipo, active, created_at) VALUES (?, ?, ?, 0, ?)",
            (cid, name, tipo, utcnow()),
        )
        aid = cur.lastrowid
    return redirect(f"/admin/instancias/{aid}", msg="Instancia creada — completá el material de corrección y activala.")


@app.get("/admin/instancias", response_class=HTMLResponse)
def admin_instancias(request: Request):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        cursos = staff_courses(db, user)
        rows = []
        for c in cursos:
            for a in db.execute(
                "SELECT a.*,"
                " (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id) AS n_entregas,"
                " (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id"
                "  AND s.kind = 'final' AND s.status = 'pendiente') AS pendientes"
                " FROM assignments a WHERE a.course_id = ? ORDER BY a.id",
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
        cursos = staff_courses(db, user)
    if not cursos:
        return redirect("/admin/instancias", err="No tenés cursos asignados para crear instancias.")
    return render(
        request, "admin_instancia_identidad.html", course=None, assignment=None,
        cursos=cursos, curso_f=_curso_param(curso),
    )


@app.post("/admin/instancias/crear")
def admin_instancia_crear_global(
    request: Request, curso_id: int = Form(...), name: str = Form(...), tipo: str = Form("abierto"),
):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    name = name.strip()
    if tipo not in ("abierto", "escrito", "choice"):
        tipo = "abierto"
    with get_db() as db:
        course = get_course(db, curso_id)
        if not course or not can_access_course(db, user, curso_id):
            return redirect("/admin/instancias", err="No podés crear instancias en ese curso.")
        if not name:
            return redirect("/admin/instancias/nueva", err="La instancia necesita un nombre (ej.: TP1, Parcial, Trabajo Final).")
        if db.execute("SELECT 1 FROM assignments WHERE course_id = ? AND name = ?", (curso_id, name)).fetchone():
            return redirect(f"/admin/instancias/nueva?curso={curso_id}", err=f"Ya existe una instancia «{name}» en ese curso.")
        cur = db.execute(
            "INSERT INTO assignments (course_id, name, tipo, active, created_at) VALUES (?, ?, ?, 0, ?)",
            (curso_id, name, tipo, utcnow()),
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
        course = get_course(db, assignment["course_id"])
        if not can_access_course(db, user, course["id"]):
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
        course = get_course(db, assignment["course_id"])
        if not can_access_course(db, user, course["id"]):
            return redirect("/admin/cursos")
        cid = course["id"]
        if action == "eliminar":
            n = db.execute("SELECT COUNT(*) n FROM submissions WHERE assignment_id = ?", (aid,)).fetchone()["n"]
            if n:
                return redirect(f"/admin/instancias/{aid}", err="Tiene entregas: no se puede eliminar.")
            db.execute("DELETE FROM assignments WHERE id = ?", (aid,))
            return redirect(f"/admin/cursos/{cid}", msg=f"Instancia «{assignment['name']}» eliminada.")
        try:
            mp = int(form.get("max_practicas", assignment["max_practicas"]))
            mq = int(form.get("max_preguntas", assignment["max_preguntas"]))
            if not (1 <= mp <= 10 and 0 <= mq <= 10):
                raise ValueError
        except ValueError:
            return redirect(f"/admin/instancias/{aid}", err="Los cupos deben ser números (prácticas 1–10, preguntas 0–10).")
        name = (form.get("name") or "").strip() or assignment["name"]
        if name != assignment["name"] and db.execute(
            "SELECT 1 FROM assignments WHERE course_id = ? AND name = ? AND id != ?", (cid, name, aid)
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
            " prompt_extra = ?, max_practicas = ?, max_preguntas = ? WHERE id = ?",
            (name, active, tipo, consigna, rubrica, respuestas,
             (form.get("prompt_extra") or "").strip(), mp, mq, aid),
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
            "SELECT u.*, (SELECT GROUP_CONCAT(c.name, ' · ') FROM course_teachers ct "
            " JOIN courses c ON c.id = ct.course_id WHERE ct.user_id = u.id) AS cursos "
            "FROM users u WHERE u.role IN ('docente', 'admin') ORDER BY u.role = 'admin' DESC, u.full_name"
        ).fetchall()
        cursos = db.execute("SELECT * FROM courses ORDER BY name").fetchall()
    return render(request, "admin_docentes.html", rows=rows, cursos=cursos)


@app.get("/admin/docentes/nuevo", response_class=HTMLResponse)
def admin_docente_nuevo(request: Request):
    user, resp = _require(request, "admin")
    if resp:
        return resp
    with get_db() as db:
        cursos = db.execute("SELECT * FROM courses ORDER BY name").fetchall()
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
            db.execute("INSERT INTO course_teachers (course_id, user_id) VALUES (?, ?)", (int(cid), uid))
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
        cursos = db.execute("SELECT * FROM courses ORDER BY name").fetchall()
        propios = {r["course_id"] for r in db.execute(
            "SELECT course_id FROM course_teachers WHERE user_id = ?", (uid,)
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
                db.execute("INSERT INTO course_teachers (course_id, user_id) VALUES (?, ?)", (int(cid), uid))
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

def _crear_o_inscribir(db, course_id: int, dni: str, nombre: str, email: str, profile: str = "") -> tuple[str, str]:
    """Devuelve (resultado, dato): ('creado', password) | ('inscripto'|'ya_estaba', nombre) | ('error', motivo)."""
    if not dni.isdigit() or not (6 <= len(dni) <= 9):
        return "error", f"DNI inválido ({dni})"
    row = db.execute("SELECT * FROM users WHERE login = ?", (dni,)).fetchone()
    if row:
        if row["role"] != "student":
            return "error", f"{dni} ya existe y no es estudiante"
        if profile:
            db.execute("UPDATE users SET profile = ? WHERE id = ?", (profile, row["id"]))
        if enroll(db, row["id"], course_id):
            return "inscripto", row["full_name"]
        return "ya_estaba", row["full_name"]
    password = auth.generate_password()
    cur = db.execute(
        "INSERT INTO users (login, password_hash, initial_password, full_name, email, role, active, profile, created_at)"
        " VALUES (?, ?, ?, ?, ?, 'student', 1, ?, ?)",
        (dni, auth.hash_password(password), password, nombre, email, profile, utcnow()),
    )
    enroll(db, cur.lastrowid, course_id)
    return "creado", password


def _alta_estudiantes(db, course_id: int, lines: list[str]):
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
        res, dato = _crear_o_inscribir(db, course_id, dni, nombre, email)
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
        cursos = staff_courses(db, user)
        ids = _scope_ids(db, user)
        if curso is not None and not can_access_course(db, user, curso):
            return redirect("/admin/estudiantes")
        if curso is not None:
            students = db.execute(
                "SELECT DISTINCT u.* FROM users u JOIN enrollments e ON e.user_id = u.id "
                "WHERE u.role = 'student' AND e.course_id = ? ORDER BY u.full_name", (curso,)
            ).fetchall()
        elif ids is None:
            students = db.execute(
                "SELECT * FROM users WHERE role = 'student' ORDER BY full_name"
            ).fetchall()
        elif ids:
            marks = ",".join("?" * len(ids))
            students = db.execute(
                f"SELECT DISTINCT u.* FROM users u JOIN enrollments e ON e.user_id = u.id "
                f"WHERE u.role = 'student' AND e.course_id IN ({marks}) ORDER BY u.full_name", ids
            ).fetchall()
        else:
            students = []
        detalle = {}
        for s in students:
            detalle[s["id"]] = db.execute(
                "SELECT c.id AS cid, c.name,"
                " (SELECT COUNT(*) FROM submissions x JOIN assignments a ON a.id = x.assignment_id"
                "  WHERE x.user_id = e.user_id AND a.course_id = c.id) AS n_entregas"
                " FROM enrollments e JOIN courses c ON c.id = e.course_id"
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
        cursos = staff_courses(db, user)
    if not cursos:
        return redirect("/admin/estudiantes", err="No tenés cursos asignados para dar altas.")
    return render(request, "admin_estudiante_nuevo.html", cursos=cursos, curso_f=_curso_param(curso))


@app.get("/admin/estudiantes/importar", response_class=HTMLResponse)
def admin_estudiantes_importar(request: Request, curso: str | None = None):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    with get_db() as db:
        cursos = staff_courses(db, user)
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
        if not can_access_course(db, user, curso_id) or not get_course(db, curso_id):
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
        if not can_access_course(db, user, curso_id) or not get_course(db, curso_id):
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
            "SELECT s.*, a.name AS instancia, c.name AS course_name FROM submissions s "
            "JOIN assignments a ON a.id = s.assignment_id JOIN courses c ON c.id = a.course_id "
            "WHERE s.user_id = ? ORDER BY s.id DESC", (uid,)
        ).fetchall()
        inscripciones = db.execute(
            "SELECT c.*, e.id AS eid,"
            " (SELECT COUNT(*) FROM submissions s JOIN assignments a ON a.id = s.assignment_id"
            "  WHERE s.user_id = ? AND a.course_id = c.id) AS n_entregas"
            " FROM enrollments e JOIN courses c ON c.id = e.course_id WHERE e.user_id = ? ORDER BY c.name",
            (uid, uid),
        ).fetchall()
        inscripto_ids = {i["id"] for i in inscripciones}
        disponibles = [c for c in staff_courses(db, user) if c["id"] not in inscripto_ids and c["active"]]
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
            if not can_access_course(db, user, curso_id) or not get_course(db, curso_id):
                return redirect(f"/admin/estudiantes/{uid}", err="No podés inscribir en ese curso.")
            if enroll(db, uid, curso_id):
                return redirect(f"/admin/estudiantes/{uid}", msg="Inscripción agregada.")
            return redirect(f"/admin/estudiantes/{uid}", msg="Ya estaba inscripto/a en ese curso.")
        if action == "desinscribir":
            if not can_access_course(db, user, curso_id):
                return redirect(f"/admin/estudiantes/{uid}", err="No podés modificar ese curso.")
            n = db.execute(
                "SELECT COUNT(*) n FROM submissions s JOIN assignments a ON a.id = s.assignment_id "
                "WHERE s.user_id = ? AND a.course_id = ?", (uid, curso_id)
            ).fetchone()["n"]
            if n:
                return redirect(
                    f"/admin/estudiantes/{uid}",
                    err="Tiene entregas en ese curso: no se puede desinscribir sin perder historial. "
                        "Si hace falta, deshabilitá al estudiante.",
                )
            db.execute("DELETE FROM enrollments WHERE user_id = ? AND course_id = ?", (uid, curso_id))
            return redirect(f"/admin/estudiantes/{uid}", msg="Inscripción quitada.")
    return redirect(f"/admin/estudiantes/{uid}")


@app.get("/admin/credenciales.csv")
def admin_credenciales(request: Request, curso: str | None = None):
    user, resp = _require(request, *STAFF)
    if resp:
        return resp
    curso = _curso_param(curso)
    with get_db() as db:
        ids = _scope_ids(db, user)
        if curso is not None and not can_access_course(db, user, curso):
            return redirect("/admin/estudiantes")
        if curso is not None:
            rows = db.execute(
                "SELECT DISTINCT u.login, u.full_name, u.email, u.initial_password FROM users u "
                "JOIN enrollments e ON e.user_id = u.id WHERE u.role = 'student' AND e.course_id = ? "
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
                f"JOIN enrollments e ON e.user_id = u.id WHERE u.role = 'student' AND e.course_id IN ({marks}) "
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
