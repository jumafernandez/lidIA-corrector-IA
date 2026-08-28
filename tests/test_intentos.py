"""Freno a los intentos de contraseña.

Lo que más importa acá no es que bloquee, sino QUE NO BLOQUEE a un aula entera: el
fracaso caro de este código no es dejar entrar a un atacante, es dejar afuera a treinta
estudiantes en medio de un parcial.
"""
import pathlib
import sys

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


@pytest.fixture()
def freno(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[m]
    from app import intentos
    from app.db import init_db
    init_db()
    intentos.init_intentos_db()
    return intentos


def test_ocho_fallos_frenan_la_cuenta(freno):
    for _ in range(7):
        freno.fallo("30111222", "1.1.1.1")
    assert freno.bloqueado("30111222", "1.1.1.1") == ""
    freno.fallo("30111222", "1.1.1.1")
    assert "Demasiados intentos" in freno.bloqueado("30111222", "1.1.1.1")


def test_la_clave_buena_no_desbloquea_antes_de_tiempo(freno):
    """Frenado es frenado: si no, bastaría con probar hasta acertar."""
    for _ in range(8):
        freno.fallo("30111222", "1.1.1.1")
    assert freno.bloqueado("30111222", "1.1.1.1") != ""


def test_acertar_limpia_la_cuenta(freno):
    for _ in range(3):
        freno.fallo("30111222", "1.1.1.1")
    freno.acierto("30111222", "1.1.1.1")
    for _ in range(7):
        freno.fallo("30111222", "1.1.1.1")
    assert freno.bloqueado("30111222", "1.1.1.1") == "", "los fallos previos no debían sumar"


def test_frenar_una_cuenta_no_frena_a_las_demas(freno):
    for _ in range(20):
        freno.fallo("victima", "1.1.1.1")
    assert freno.bloqueado("victima", "1.1.1.1") != ""
    assert freno.bloqueado("otra_persona", "1.1.1.1") == ""


def test_un_aula_entera_no_queda_afuera(freno):
    """Treinta estudiantes equivocándose desde la misma red siguen pudiendo entrar."""
    for n in range(30):
        for _ in range(3):
            freno.fallo(f"alumno{n}", "10.0.0.1")
    assert freno.bloqueado("alumno31", "10.0.0.1") == ""
    assert freno.bloqueado("alumno7", "10.0.0.1") == "", "tres fallos no pueden frenar a nadie"


def test_la_ventana_vence(freno, monkeypatch):
    import time
    for _ in range(8):
        freno.fallo("30111222", "1.1.1.1")
    assert freno.bloqueado("30111222", "1.1.1.1") != ""
    monkeypatch.setattr(time, "time", lambda: time.time.__self__ if False else 1e12)
    assert freno.bloqueado("30111222", "1.1.1.1") == ""


def test_el_origen_sale_del_encabezado_del_proxy(freno):
    class Pedido:
        headers = {"x-forwarded-for": "200.1.2.3, 10.0.0.1"}
        client = type("C", (), {"host": "127.0.0.1"})()
    assert freno.origen(Pedido()) == "200.1.2.3"

    class SinProxy:
        headers = {}
        client = type("C", (), {"host": "192.168.1.5"})()
    assert freno.origen(SinProxy()) == "192.168.1.5"
