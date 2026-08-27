"""Envío de correo (opcional): notifica la devolución final aprobada."""
import os
import smtplib
from email.message import EmailMessage


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST"))


def send_feedback_email(to_addr: str, student_name: str, feedback_md: str) -> tuple[bool, str]:
    """Devuelve (enviado, detalle). Si SMTP no está configurado, no falla: informa."""
    if not to_addr:
        return False, "El estudiante no tiene correo cargado."
    if not smtp_configured():
        return False, "SMTP no configurado (variables SMTP_*): la devolución queda disponible solo en la aplicación."

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("SMTP_FROM", user or "lidia@licdia.unlu.edu.ar")
    use_tls = os.environ.get("SMTP_TLS", "1") == "1"

    msg = EmailMessage()
    msg["Subject"] = "Devolución de tu Trabajo Final — Diplomatura en IA Generativa"
    msg["From"] = sender
    msg["To"] = to_addr
    msg.set_content(
        f"Hola {student_name}:\n\n"
        "El equipo docente aprobó la devolución de tu entrega final. La copiamos abajo; "
        "también podés verla ingresando a la plataforma.\n\n"
        "----------------------------------------\n\n"
        f"{feedback_md}\n\n"
        "----------------------------------------\n"
        "Diplomatura de Posgrado en IA Generativa — LICDIA · UNLu"
    )
    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            if use_tls:
                server.starttls()
            if user:
                server.login(user, password)
            server.send_message(msg)
        return True, f"Correo enviado a {to_addr}."
    except Exception as exc:  # noqa: BLE001
        return False, f"No se pudo enviar el correo: {exc}"
