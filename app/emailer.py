"""Correo saliente de LidIA.

Cada situación tiene su propio mensaje: el asunto, el cuerpo y lo que se explica cambian
según lo que el correo comunica. Nada de plantillas genéricas que sirvan para todo y no
digan nada — quien recibe tiene que entender de qué materia, de qué cursada y de qué
entrega le están hablando sin abrir la aplicación.

Todos salen de una dirección automática que nadie lee: el canal con el estudiantado es el
equipo docente, no este buzón. El pie lo dice sin dar vueltas.

Los mensajes van en dos partes, texto y HTML. La devolución se escribe en Markdown, así
que en texto plano llegaría con los asteriscos y los numerales a la vista; la parte HTML
la muestra como corresponde y los clientes que no la soportan siguen viendo el texto.
"""
import os
import re
import smtplib
from email.message import EmailMessage

import markdown as md_lib


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def desvio() -> str:
    """Dirección a la que se desvía TODO el correo mientras se prueba, o cadena vacía.

    Con esto se puede probar el circuito completo sin escribirle a nadie real. El
    mensaje desviado dice a quién le hubiera llegado, y la aplicación avisa en pantalla
    que el desvío está puesto: si queda prendido en producción, se ve.
    """
    return os.environ.get("SMTP_DESVIAR_A", "").strip()


def url_absoluta(ruta: str = "") -> str:
    """Dirección pública de una ruta, para los enlaces de los correos."""
    return _app_url(ruta)


def _app_url(ruta: str = "") -> str:
    """URL pública de LidIA, si está declarada. Sin ella los correos no llevan enlace."""
    base = os.environ.get("APP_URL", "").strip().rstrip("/")
    return f"{base}{ruta}" if base else ""


def _cursada(course) -> str:
    """«Introducción a la Inteligencia Artificial 2026», tomado de los datos reales."""
    try:
        return f"{course['materia']} {course['etiqueta']}".strip()
    except (KeyError, TypeError, IndexError):
        return ""


AVISO = ("Este es un correo automático de LidIA y nadie lee las respuestas. "
         "Si tenés dudas sobre esta devolución, hablalas con el equipo docente.")

_ESTILO = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:15px;
line-height:1.6;color:#212529;margin:0;padding:24px}
.caja{max-width:640px;margin:0 auto}
.enc{border-left:4px solid #0e6b74;padding-left:14px;margin-bottom:22px}
.enc h1{font-size:17px;margin:0 0 4px;color:#0e6b74}
.enc p{margin:0;font-size:13px;color:#6c757d}
.nota{display:inline-block;background:#e3f4f5;color:#0a4d54;border-radius:6px;
padding:6px 12px;font-weight:700;margin:14px 0}
.dev{border-top:1px solid #dee2e6;border-bottom:1px solid #dee2e6;padding:18px 0;margin:18px 0}
.dev h1,.dev h2,.dev h3{font-size:15px;color:#0a4d54;margin:18px 0 6px}
.dev p,.dev li{margin:6px 0}
.pie{font-size:12px;color:#6c757d;margin-top:22px}
a{color:#0e6b74}
"""


def _armar(asunto: str, encabezado: str, subtitulo: str, cuerpo_txt: str,
           cuerpo_html: str, enlace: str = "", enlace_txt: str = "") -> tuple:
    """Devuelve (asunto, texto, html) con el marco común de todos los correos."""
    pie_txt = f"\n{'-' * 44}\n{subtitulo}\nLICDIA · Universidad Nacional de Luján\n\n{AVISO}\n"
    txt = f"{cuerpo_txt}\n"
    if enlace:
        txt += f"\n{enlace_txt}: {enlace}\n"
    txt += pie_txt

    boton = (f'<p><a href="{enlace}">{enlace_txt}</a></p>' if enlace else "")
    html = (f'<html><head><meta charset="utf-8"><style>{_ESTILO}</style></head><body>'
            f'<div class="caja">'
            f'<div class="enc"><h1>{encabezado}</h1><p>{subtitulo}</p></div>'
            f'{cuerpo_html}{boton}'
            f'<p class="pie">LICDIA · Universidad Nacional de Luján<br>{AVISO}</p>'
            f'</div></body></html>')
    return asunto, txt, html


def _md(texto: str) -> str:
    return md_lib.markdown(texto or "", extensions=["extra", "sane_lists"])


def _sin_md(texto: str) -> str:
    """Markdown a texto legible: saca los asteriscos y numerales que en un mail estorban."""
    t = re.sub(r"^#{1,6}\s*", "", texto or "", flags=re.M)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*", r"\1", t)
    return t


# ------------------------------------------------------------------ situaciones

def devolucion_aprobada(alumno: str, course, assignment, feedback_md: str, nota=None) -> tuple:
    """El equipo docente firmó la corrección de una entrega final."""
    cursada = _cursada(course)
    inst = assignment["name"]
    asunto = f"Tu devolución de «{inst}» — {cursada}" if cursada else f"Tu devolución de «{inst}»"

    intro = (f"Hola {alumno}: el equipo docente revisó y firmó la devolución de tu entrega "
             f"de «{inst}».")
    nota_txt = "" if nota is None else f"\n\nNota: {nota:g} (de 10)."
    nota_html = "" if nota is None else f'<p class="nota">Nota: {nota:g} de 10</p>'

    return _armar(
        asunto, f"Devolución de «{inst}»", cursada,
        f"{intro}{nota_txt}\n\n{'-' * 44}\n\n{_sin_md(feedback_md)}",
        f"<p>{intro}</p>{nota_html}<div class=\"dev\">{_md(feedback_md)}</div>",
        _app_url("/panel"), "Ver la devolución en LidIA",
    )


def entrega_reabierta(alumno: str, course, assignment, motivo: str = "") -> tuple:
    """El equipo docente reabrió la entrega: el estudiante puede volver a presentar."""
    cursada = _cursada(course)
    inst = assignment["name"]
    intro = (f"Hola {alumno}: el equipo docente reabrió tu entrega de «{inst}». "
             f"Podés corregirla y volver a presentarla.")
    extra_txt = f"\n\nComentario del equipo docente:\n{motivo}" if motivo else ""
    extra_html = (f'<div class="dev">{_md(motivo)}</div>' if motivo else "")

    return _armar(
        f"Reabrimos tu entrega de «{inst}» — {cursada}" if cursada else f"Reabrimos tu entrega de «{inst}»",
        f"Entrega reabierta: «{inst}»", cursada,
        f"{intro}{extra_txt}",
        f"<p>{intro}</p>{extra_html}",
        _app_url("/panel"), "Volver a presentar en LidIA",
    )


# ------------------------------------------------------------------ envío

def invitacion(nombre: str, login: str, enlace: str) -> tuple:
    """Primer ingreso: la cuenta existe y falta que su dueño le ponga contraseña."""
    saludo = f"Hola{', ' + nombre if nombre else ''}:"
    txt = (f"{saludo}\n\n"
           f"Te damos de alta en LidIA, el sistema de entregas y devoluciones del LICDIA.\n\n"
           f"Tu usuario es {login}. La contraseña la elegís vos con este enlace, que sirve una "
           f"sola vez y vence en una semana.\n\n"
           f"Si no lo usás a tiempo, pedí uno nuevo desde «¿Olvidaste tu contraseña?» "
           f"en la pantalla de ingreso.")
    html = (f"<p>{saludo}</p>"
            f"<p>Te damos de alta en <strong>LidIA</strong>, el sistema de entregas y devoluciones "
            f"del LICDIA.</p>"
            f"<p>Tu usuario es <strong>{login}</strong>. La contraseña la elegís vos con este "
            f"enlace, que sirve una sola vez y vence en una semana.</p>"
            f"<p class=\"chico\">Si no lo usás a tiempo, pedí uno nuevo desde "
            f"«¿Olvidaste tu contraseña?» en la pantalla de ingreso.</p>")
    return _armar("Tu acceso a LidIA", "Bienvenido a LidIA",
                  "Entregas y devoluciones · LICDIA", txt, html,
                  enlace, "Elegir mi contraseña")


def recuperacion(nombre: str, login: str, enlace: str) -> tuple:
    """Olvido: alguien pidió volver a entrar y hay que dejarlo elegir una nueva."""
    saludo = f"Hola{', ' + nombre if nombre else ''}:"
    txt = (f"{saludo}\n\n"
           f"Alguien pidió restablecer la contraseña de {login} en LidIA. Si fuiste vos, "
           f"usá este enlace: sirve una sola vez y vence en dos horas.\n\n"
           f"Si no fuiste vos, ignorá este mensaje. Tu contraseña actual sigue funcionando "
           f"y nadie más puede usar el enlace.")
    html = (f"<p>{saludo}</p>"
            f"<p>Alguien pidió restablecer la contraseña de <strong>{login}</strong> en LidIA. "
            f"Si fuiste vos, usá este enlace: sirve una sola vez y vence en dos horas.</p>"
            f"<p class=\"chico\">Si no fuiste vos, ignorá este mensaje. Tu contraseña actual "
            f"sigue funcionando y nadie más puede usar el enlace.</p>")
    return _armar("Restablecer tu contraseña de LidIA", "Restablecer contraseña",
                  "Entregas y devoluciones · LICDIA", txt, html,
                  enlace, "Elegir una contraseña nueva")


def enviar(to_addr: str, mensaje: tuple) -> tuple[bool, str]:
    """Manda un mensaje ya armado. Devuelve (enviado, detalle); nunca lanza."""
    asunto, txt, html = mensaje
    if not to_addr:
        return False, "No se envió ningún correo: no hay dirección cargada."
    if not smtp_configured():
        return False, "No se envió ningún correo: el servidor de correo no está configurado."

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("SMTP_FROM", user or "lidia@licdia.unlu.edu.ar")
    use_tls = os.environ.get("SMTP_TLS", "1") == "1"

    destino, real = to_addr, ""
    if d := desvio():
        destino, real = d, to_addr
        asunto = f"[desviado · era para {real}] {asunto}"
        nota = (f"Este correo estaba dirigido a {real} y se desvió acá porque LidIA está "
                f"en modo de prueba (SMTP_DESVIAR_A).")
        txt = f"*** {nota} ***\n\n{txt}"
        html = html.replace(
            '<div class="caja">',
            f'<div class="caja"><p style="background:#fff4cc;color:#8a5a00;padding:10px 12px;'
            f'border-radius:6px;font-size:13px;margin:0 0 18px">{nota}</p>', 1)

    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = f"LidIA · LICDIA <{sender}>"
    msg["To"] = destino
    msg["Auto-Submitted"] = "auto-generated"   # que los autorespondedores no contesten
    msg.set_content(txt)
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            if use_tls:
                server.starttls()
            if user:
                server.login(user, password)
            server.send_message(msg)
        return True, f"Se envió un correo a {to_addr}."
    except Exception as exc:  # noqa: BLE001
        return False, f"No se pudo enviar el correo: {exc}"
