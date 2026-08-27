"""Exportación de datos para investigación educativa.

Un caso por entrega, con las variables que no se pueden reconstruir después: la
configuración exacta con la que se generó la devolución, el costo del modelo, cuánto la
editó el equipo docente antes de firmarla, si el estudiantado la leyó y si le sirvió.

Las personas van seudonimizadas con un HMAC de sal fija por instalación: el mismo
estudiante conserva el mismo código entre exportaciones (hace falta para seguir su
trayectoria a lo largo del cuatrimestre) pero el código no permite volver a la persona.
Solo se exportan quienes dieron consentimiento explícito desde «Tu cuenta».
"""
import csv
import hashlib
import hmac
import io
import secrets
from datetime import datetime, timezone

from .db import get_config, set_config

CAMPOS = [
    "caso", "estudiante", "docente",
    "materia", "edicion", "edicion_inicio", "edicion_fin",
    "instancia", "tipo", "modalidad", "grupal", "integrantes",
    "intento", "nro_intento", "entregada_el", "dia_de_cursada",
    "caracteres", "truncada",
    "modelo", "tokens_entrada", "tokens_salida", "latencia_ms", "corte",
    "estado", "caracteres_ia", "caracteres_final", "reescritura_docente",
    "corregida_el", "horas_hasta_correccion", "nota",
    "leida_el", "horas_hasta_leer", "valoracion", "comentario",
]


def _sal(db) -> bytes:
    """Sal de seudonimización, estable por instalación y creada la primera vez que se usa."""
    cfg = get_config(db)
    sal = cfg.get("pseudo_salt")
    if not sal:
        sal = secrets.token_hex(32)
        set_config(db, "pseudo_salt", sal)
    return sal.encode()


def seudonimo(sal: bytes, prefijo: str, uid) -> str:
    if uid is None:
        return ""
    firma = hmac.new(sal, f"{prefijo}:{uid}".encode(), hashlib.sha256).hexdigest()
    return f"{prefijo}{firma[:10]}"


def _dt(txt):
    if not txt:
        return None
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _horas(desde, hasta):
    a, b = _dt(desde), _dt(hasta)
    if not a or not b:
        return ""
    return f"{(b - a).total_seconds() / 3600:.2f}"


def _dia_de_cursada(inicio, entregada):
    """Día de la cursada en que llegó la entrega: 1 = primer día. Vacío si la edición no tiene fechas."""
    if not inicio:
        return ""
    b = _dt(entregada)
    if not b:
        return ""
    try:
        a = datetime.fromisoformat(inicio).replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return str((b.date() - a.date()).days + 1)


CONSULTA = """
SELECT s.*, u.id AS uid, u.consent,
       a.name AS instancia, a.tipo, a.max_integrantes,
       c.name AS materia, ed.etiqueta AS edicion, ed.fecha_inicio, ed.fecha_fin,
       (SELECT COUNT(*) FROM grupo_miembros gm WHERE gm.grupo_id = s.grupo_id) AS integrantes
  FROM submissions s
  JOIN users u ON u.id = s.user_id
  JOIN assignments a ON a.id = s.assignment_id
  JOIN course_editions ed ON ed.id = a.edition_id
  JOIN courses c ON c.id = ed.course_id
 WHERE u.consent = 1
 ORDER BY s.id
"""


def filas(db):
    sal = _sal(db)
    orden = {}
    for r in db.execute(CONSULTA).fetchall():
        # el número de intento se cuenta por separado en prácticas y en finales: una final
        # no es «el tercer intento», es la única de su serie
        clave = (r["grupo_id"] or f"u{r['uid']}", r["assignment_id"], r["kind"])
        orden[clave] = orden.get(clave, 0) + 1
        ia = r["ai_feedback_md"] or ""
        final = r["final_feedback_md"] or ""
        yield {
            "caso": r["id"],
            "estudiante": seudonimo(sal, "E", r["uid"]),
            "docente": seudonimo(sal, "D", r["reviewed_by"]),
            "materia": r["materia"],
            "edicion": r["edicion"],
            "edicion_inicio": r["fecha_inicio"] or "",
            "edicion_fin": r["fecha_fin"] or "",
            "instancia": r["instancia"],
            "tipo": r["tipo"],
            "modalidad": "grupal" if r["grupo_id"] else "individual",
            "grupal": 1 if r["grupo_id"] else 0,
            "integrantes": r["integrantes"] or 1,
            "intento": r["kind"],
            "nro_intento": orden[clave],
            "entregada_el": r["created_at"],
            "dia_de_cursada": _dia_de_cursada(r["fecha_inicio"], r["created_at"]),
            "caracteres": r["text_chars"],
            "truncada": r["truncated"],
            "modelo": r["model_used"] or "",
            "tokens_entrada": r["tokens_in"] if r["tokens_in"] is not None else "",
            "tokens_salida": r["tokens_out"] if r["tokens_out"] is not None else "",
            "latencia_ms": r["latencia_ms"] if r["latencia_ms"] is not None else "",
            "corte": r["finish_reason"] or "",
            "estado": r["status"],
            "caracteres_ia": len(ia),
            "caracteres_final": len(final),
            "reescritura_docente": "" if r["edit_ratio"] is None else f"{r['edit_ratio']:.4f}",
            "corregida_el": r["reviewed_at"] or "",
            "horas_hasta_correccion": _horas(r["created_at"], r["reviewed_at"]),
            "nota": "" if r["nota"] is None else f"{r['nota']:g}",
            "leida_el": r["first_viewed_at"] or "",
            "horas_hasta_leer": _horas(r["created_at"], r["first_viewed_at"]),
            "valoracion": r["valoracion"] if r["valoracion"] is not None else "",
            "comentario": (r["valoracion_texto"] or "").replace("\n", " "),
        }


def csv_datos(db) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CAMPOS, extrasaction="ignore")
    w.writeheader()
    for fila in filas(db):
        w.writerow(fila)
    return buf.getvalue()


def csv_configuraciones(db) -> str:
    """Las consignas y rúbricas con que se generó cada devolución, una fila por entrega.

    Va aparte porque son textos largos: en la misma tabla harían ilegible el CSV principal.
    """
    sal = _sal(db)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["caso", "estudiante", "consigna", "rubrica", "respuestas_esperadas", "prompt_extra"])
    for r in db.execute(CONSULTA).fetchall():
        import json as _json
        try:
            cfg = _json.loads(r["cfg_snapshot"]) if r["cfg_snapshot"] else {}
        except (ValueError, TypeError):
            cfg = {}
        if not cfg:
            continue
        w.writerow([r["id"], seudonimo(sal, "E", r["uid"]),
                    cfg.get("consigna", ""), cfg.get("rubrica", ""),
                    cfg.get("respuestas", ""), cfg.get("prompt_extra", "")])
    return buf.getvalue()


def resumen(db) -> dict:
    """Lo que hoy hay para exportar, para no descubrir en diciembre que faltaba una variable."""
    def uno(sql, *args):
        fila = db.execute(sql, args).fetchone()
        return fila[0] if fila else 0

    total_est = uno("SELECT COUNT(*) FROM users WHERE role = 'student' AND active = 1")
    return {
        "estudiantes": total_est,
        "consintieron": uno("SELECT COUNT(*) FROM users WHERE role = 'student' AND consent = 1"),
        "rechazaron": uno("SELECT COUNT(*) FROM users WHERE role = 'student' AND consent = 0"),
        "sin_responder": uno(
            "SELECT COUNT(*) FROM users WHERE role = 'student' AND active = 1 AND consent IS NULL"),
        "entregas": uno("SELECT COUNT(*) FROM submissions"),
        "exportables": uno(
            "SELECT COUNT(*) FROM submissions s JOIN users u ON u.id = s.user_id WHERE u.consent = 1"),
        "corregidas": uno("SELECT COUNT(*) FROM submissions WHERE status = 'aprobada'"),
        "con_reescritura": uno("SELECT COUNT(*) FROM submissions WHERE edit_ratio IS NOT NULL"),
        "reescritura_media": uno("SELECT ROUND(AVG(edit_ratio), 3) FROM submissions WHERE edit_ratio IS NOT NULL"),
        "leidas": uno("SELECT COUNT(*) FROM submissions WHERE first_viewed_at IS NOT NULL"),
        "valoradas": uno("SELECT COUNT(*) FROM submissions WHERE valoracion IS NOT NULL"),
        "utiles": uno("SELECT COUNT(*) FROM submissions WHERE valoracion = 1"),
        "con_telemetria": uno("SELECT COUNT(*) FROM submissions WHERE tokens_in IS NOT NULL"),
        "ediciones_sin_fechas": uno(
            "SELECT COUNT(*) FROM course_editions WHERE active = 1 AND COALESCE(fecha_inicio, '') = ''"),
    }
