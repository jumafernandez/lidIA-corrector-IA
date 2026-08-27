"""Crea el usuario de coordinación y, opcionalmente, datos de demostración.

Uso normal (instalación nueva):

    LIDIA_ADMIN_LOGIN=30111222 LIDIA_ADMIN_NAME="Apellido, Nombre" python seed.py

La contraseña se genera al azar y se imprime una sola vez. Con --demo se agregan además
un curso de ejemplo con una instancia activa y tres estudiantes de prueba, para mostrar
el sistema sin cargar datos reales.
"""
import os
import sys

from app import auth
from app.db import DEMO_CONSIGNA, DEMO_RUBRICA, enroll, get_db, init_db, utcnow

ADMIN_LOGIN = os.environ.get("LIDIA_ADMIN_LOGIN", "admin")
ADMIN_NAME = os.environ.get("LIDIA_ADMIN_NAME", "Coordinación")
ADMIN_EMAIL = os.environ.get("LIDIA_ADMIN_EMAIL", "")

CURSO_DEMO = "Curso de demostración"
INSTANCIA_DEMO = "Trabajo Final Integrador"

ESTUDIANTES_DEMO = [
    ("30111222", "Gómez, María", "mgomez@ejemplo.com"),
    ("28333444", "Pérez, Juan", "jperez@ejemplo.com"),
    ("33555666", "Suárez, Ana", ""),
]


def crear_admin(db) -> bool:
    if db.execute("SELECT 1 FROM users WHERE login = ?", (ADMIN_LOGIN,)).fetchone():
        print(f"Ya existe el usuario «{ADMIN_LOGIN}»; no se cambia nada.")
        return False
    password = auth.generate_password()
    db.execute(
        "INSERT INTO users (login, password_hash, initial_password, full_name, email, role, active, created_at)"
        " VALUES (?, ?, ?, ?, ?, 'admin', 1, ?)",
        (ADMIN_LOGIN, auth.hash_password(password), password, ADMIN_NAME, ADMIN_EMAIL, utcnow()),
    )
    print(f"Coordinación creada → usuario: {ADMIN_LOGIN} · contraseña: {password}")
    print("Anotala ahora: se muestra una sola vez. Cambiala al entrar, desde «Tu cuenta».")
    return True


def crear_demo(db):
    if db.execute("SELECT 1 FROM courses WHERE name = ?", (CURSO_DEMO,)).fetchone():
        print("Los datos de demostración ya existen.")
        return
    cur = db.execute(
        "INSERT INTO courses (name, active, created_at) VALUES (?, 1, ?)", (CURSO_DEMO, utcnow())
    )
    curso_id = cur.lastrowid
    db.execute(
        "INSERT INTO assignments (course_id, name, active, consigna, rubrica, created_at)"
        " VALUES (?, ?, 1, ?, ?, ?)",
        (curso_id, INSTANCIA_DEMO, DEMO_CONSIGNA, DEMO_RUBRICA, utcnow()),
    )
    print(f"Curso de demostración: {CURSO_DEMO} — instancia: {INSTANCIA_DEMO}")
    for dni, nombre, email in ESTUDIANTES_DEMO:
        password = auth.generate_password()
        cur = db.execute(
            "INSERT INTO users (login, password_hash, initial_password, full_name, email, role, active, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'student', 1, ?)",
            (dni, auth.hash_password(password), password, nombre, email, utcnow()),
        )
        enroll(db, cur.lastrowid, curso_id)
        print(f"  Estudiante {nombre} → DNI: {dni} · contraseña: {password}")


def main():
    init_db()
    with get_db() as db:
        crear_admin(db)
        if "--demo" in sys.argv:
            crear_demo(db)
        else:
            print("(Para cargar un curso y estudiantes de ejemplo: python seed.py --demo)")


if __name__ == "__main__":
    main()
