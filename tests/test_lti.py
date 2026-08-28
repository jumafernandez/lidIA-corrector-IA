"""Pruebas de la integración con el campus.

No hace falta Moodle: se levanta una plataforma falsa que firma tokens de verdad con
su propia clave RSA, que es exactamente lo que hace Moodle. Así las defensas se prueban
sin depender de tener un contenedor arriba.
"""
import json
import os
import pathlib
import sys
import tempfile
import time
import uuid

import pytest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


@pytest.fixture()
def entorno(tmp_path, monkeypatch):
    """Base limpia, claves nuevas y una plataforma falsa registrada."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    def par():
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv = k.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()).decode()
        pub = k.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        return k, priv, pub

    _, priv_tool, pub_tool = par()
    clave_plataforma, priv_plat, pub_plat = par()

    (tmp_path / "privada.pem").write_text(priv_tool)
    (tmp_path / "publica.pem").write_text(pub_tool)
    plataformas = {
        "https://campus.ejemplo": {
            "default": True,
            "client_id": "CLIENTE-1",
            "auth_login_url": "https://campus.ejemplo/auth",
            "auth_token_url": "https://campus.ejemplo/token",
            "key_set_url": None,
            "key_set": {"keys": []},
            "private_key_file": str(tmp_path / "privada.pem"),
            "public_key_file": str(tmp_path / "publica.pem"),
            "deployment_ids": ["1"],
        }
    }
    (tmp_path / "plataformas.json").write_text(json.dumps(plataformas))

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LTI_PLATAFORMAS", str(tmp_path / "plataformas.json"))
    monkeypatch.setenv("LTI_CLAVE_PRIVADA", str(tmp_path / "privada.pem"))
    monkeypatch.setenv("LTI_CLAVE_PUBLICA", str(tmp_path / "publica.pem"))

    for m in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[m]

    from app import lti, lti_storage
    from app.db import init_db
    init_db()
    lti_storage.init_lti_db()
    return {"dir": tmp_path, "clave_plataforma": clave_plataforma, "pub_plat": pub_plat,
            "lti": lti, "storage": lti_storage}


def _token(entorno, **cambios):
    """Un id_token firmado como lo firmaría el campus."""
    import jwt as pyjwt
    C = "https://purl.imsglobal.org/spec/lti/claim"
    ahora = int(time.time())
    cuerpo = {
        "iss": "https://campus.ejemplo",
        "aud": "CLIENTE-1",
        "sub": "usuario-7",
        "exp": ahora + 60,
        "iat": ahora,
        "nonce": uuid.uuid4().hex,
        "name": "Prueba Prueba",
        f"{C}/deployment_id": "1",
        f"{C}/message_type": "LtiResourceLinkRequest",
        f"{C}/version": "1.3.0",
        f"{C}/roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"],
        f"{C}/context": {"id": "curso-1", "title": "Curso de prueba"},
        f"{C}/resource_link": {"id": "enlace-1"},
        f"{C}/custom": {"lidia_dni": "11222333"},
        f"{C}/target_link_uri": "http://testserver/lti/launch",
    }
    cuerpo.update(cambios)
    return pyjwt.encode(cuerpo, entorno["clave_plataforma"], algorithm="RS256",
                        headers={"kid": "clave-plataforma"})


def test_jwks_publica_una_clave(entorno):
    """La herramienta publica su clave pública para que el campus la valide."""
    jwks = entorno["lti"].jwks()
    assert len(jwks["keys"]) == 1
    assert jwks["keys"][0]["kty"] == "RSA"
    assert jwks["keys"][0]["alg"] == "RS256"


def test_destino_ajeno_se_rechaza(entorno):
    """/lti/login no puede usarse como redirector abierto hacia cualquier lado."""
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    r = c.post("/lti/login", data={
        "iss": "https://campus.ejemplo", "login_hint": "7",
        "target_link_uri": "https://sitio-ajeno.example/robar",
        "client_id": "CLIENTE-1",
    })
    assert r.status_code == 400
    assert "no reconoce" in r.text or "no reconocido" in r.text


def test_nonce_es_de_un_solo_uso(entorno):
    """Reproducir un lanzamiento capturado tiene que fallar la segunda vez."""
    a = entorno["storage"].AlmacenSqlite()
    a.set_value("lti1p3-nonce-xyz", "1", 600)
    assert a.check_value("lti1p3-nonce-xyz") is True
    assert a.check_value("lti1p3-nonce-xyz") is False


def test_el_state_no_se_consume(entorno):
    """El state sí se puede leer más de una vez dentro del mismo lanzamiento."""
    a = entorno["storage"].AlmacenSqlite()
    a.set_value("lti1p3-state-abc", {"x": 1}, 600)
    assert a.get_value("lti1p3-state-abc") == {"x": 1}
    assert a.get_value("lti1p3-state-abc") == {"x": 1}


def test_lo_vencido_no_sirve(entorno):
    a = entorno["storage"].AlmacenSqlite()
    a.set_value("lti1p3-state-viejo", {"x": 1}, -1)
    assert a.get_value("lti1p3-state-viejo") is None


def test_custom_sin_sustituir_se_descarta(entorno):
    """Si el campus manda «$User.username» literal, no es un DNI: se ignora."""
    C = "https://purl.imsglobal.org/spec/lti/claim"
    limpio = entorno["lti"]._custom({f"{C}/custom": {
        "lidia_dni": "$User.username", "lidia_instancia": "4"}})
    assert "lidia_dni" not in limpio
    assert limpio["lidia_instancia"] == "4"


def test_roles_docente_se_reconocen(entorno):
    C = "https://purl.imsglobal.org/spec/lti/claim"
    docente = {f"{C}/roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"]}
    alumno = {f"{C}/roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"]}
    assert entorno["lti"]._es_docente(docente) is True
    assert entorno["lti"]._es_docente(alumno) is False


def test_identidad_manda_sobre_el_dni(entorno):
    """Si el DNI cambia en el campus, la atadura previa sigue valiendo."""
    from app.db import get_db, utcnow
    with get_db() as db:
        uid = db.execute(
            "INSERT INTO users (login, password_hash, full_name, role, active, created_at)"
            " VALUES ('11222333', 'x', 'Prueba', 'student', 1, ?)", (utcnow(),)).lastrowid
    entorno["storage"].vincular_identidad(
        "https://campus.ejemplo", "CLIENTE-1", "1", "usuario-7", uid, "11222333")

    C = "https://purl.imsglobal.org/spec/lti/claim"
    datos = {"iss": "https://campus.ejemplo", "aud": "CLIENTE-1", "sub": "usuario-7",
             f"{C}/deployment_id": "1"}
    fila, motivo = entorno["lti"]._resolver_usuario(datos, {"lidia_dni": "OTRO-DNI-DISTINTO"})
    assert fila is not None and fila["id"] == uid, motivo


def test_dni_desconocido_no_crea_usuario(entorno):
    """Sin alta automática: un DNI que no está en LidIA no entra."""
    from app.db import get_db
    C = "https://purl.imsglobal.org/spec/lti/claim"
    datos = {"iss": "https://campus.ejemplo", "aud": "CLIENTE-1", "sub": "otro-sub",
             f"{C}/deployment_id": "1"}
    fila, motivo = entorno["lti"]._resolver_usuario(datos, {"lidia_dni": "99887766"})
    assert fila is None
    assert "99887766" in motivo
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM users WHERE login='99887766'").fetchone()[0] == 0


def test_despliegue_no_habilitado_se_rechaza(entorno):
    """Un deployment_id que no registramos no puede lanzar."""
    assert entorno["lti"]._despliegues("https://campus.ejemplo") == ["1"]
    assert "99" not in entorno["lti"]._despliegues("https://campus.ejemplo")


def test_cliente_ajeno_no_esta_habilitado(entorno):
    assert entorno["lti"]._clientes("https://campus.ejemplo") == ["CLIENTE-1"]
    assert entorno["lti"]._clientes("https://otro-campus.ejemplo") == []


def test_cookie_del_apreton_va_para_entre_sitios(entorno):
    """La cookie del state tiene que salir Secure y SameSite=None o el lanzamiento muere."""
    from starlette.requests import Request
    from starlette.responses import Response
    from app.lti_starlette import CookieServiceStarlette, PedidoStarlette

    scope = {"type": "http", "method": "POST", "scheme": "http", "path": "/lti/login",
             "headers": [], "query_string": b"", "server": ("127.0.0.1", 8080)}
    pedido = PedidoStarlette(Request(scope), form={})
    cookies = CookieServiceStarlette(pedido)
    cookies.set_cookie("state-1", "v")
    resp = cookies.update_response(Response())
    cabecera = [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"][0]
    assert "SameSite=none" in cabecera
    assert "Secure" in cabecera
    assert "HttpOnly" in cabecera


# ------------------------------------------------------------------ servicios del campus

def test_lista_normaliza_roles_y_estado(entorno, monkeypatch):
    """Lo que devuelve el campus se traduce a algo con lo que se pueda trabajar."""
    from app import lti_servicios

    class RespuestaFalsa:
        status_code = 200
        headers = {}
        @staticmethod
        def json():
            return {"members": [
                {"user_id": "5", "name": "María Gómez", "email": "m@e.com",
                 "ext_user_username": "30111222", "status": "Active",
                 "roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"]},
                {"user_id": "3", "name": "Juan Docente", "ext_user_username": "jdoc",
                 "roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"]},
                {"user_id": "9", "name": "Baja Baja", "ext_user_username": "40000000",
                 "status": "Inactive",
                 "roles": ["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"]},
            ]}

    monkeypatch.setattr(lti_servicios, "token", lambda *a, **k: "t")
    monkeypatch.setattr(lti_servicios.requests, "get", lambda *a, **k: RespuestaFalsa())
    gente = lti_servicios.lista("https://campus.ejemplo", "https://campus.ejemplo/lista")

    assert len(gente) == 3
    alumna = [g for g in gente if g["usuario"] == "30111222"][0]
    assert alumna["estudiante"] and not alumna["docente"] and alumna["activo"]
    docente = [g for g in gente if g["usuario"] == "jdoc"][0]
    assert docente["docente"] and not docente["estudiante"]
    baja = [g for g in gente if g["usuario"] == "40000000"][0]
    assert baja["estudiante"] and not baja["activo"]


def test_nota_sin_registrar_se_detecta(entorno, monkeypatch):
    """Si el campus devuelve el resultado sin puntaje, la nota no quedó."""
    from app import lti_servicios

    class SinPuntaje:
        status_code = 200
        @staticmethod
        def json():
            return [{"id": "x", "userId": "5"}]          # sin resultScore

    class ConPuntaje:
        status_code = 200
        @staticmethod
        def json():
            return [{"id": "x", "userId": "5", "resultScore": 7.5, "resultMaximum": 10}]

    monkeypatch.setattr(lti_servicios, "token", lambda *a, **k: "t")

    monkeypatch.setattr(lti_servicios.requests, "get", lambda *a, **k: SinPuntaje())
    assert lti_servicios.quedo_registrada("https://campus.ejemplo", "https://c/li", "5") == (False, None)

    monkeypatch.setattr(lti_servicios.requests, "get", lambda *a, **k: ConPuntaje())
    assert lti_servicios.quedo_registrada("https://campus.ejemplo", "https://c/li", "5") == (True, 7.5)


def test_no_se_puede_confirmar_no_es_lo_mismo_que_fallar(entorno, monkeypatch):
    from app import lti_servicios

    class Rota:
        status_code = 500
        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(lti_servicios, "token", lambda *a, **k: "t")
    monkeypatch.setattr(lti_servicios.requests, "get", lambda *a, **k: Rota())
    quedo, _ = lti_servicios.quedo_registrada("https://campus.ejemplo", "https://c/li", "5")
    assert quedo is None, "no poder comprobar tiene que ser distinto de haber fallado"


def test_nombre_se_da_vuelta_para_lidia(entorno):
    from app.lti import _apellido_nombre
    assert _apellido_nombre("María Gómez") == "Gómez, María"
    assert _apellido_nombre("Juan Manuel Fernández") == "Fernández, Juan Manuel"
    assert _apellido_nombre("Prince") == "Prince"


def test_cotejo_separa_los_que_no_cruzan_de_los_que_no_estan(entorno):
    """Distinguir «no está en el campus» de «no lo pude cruzar» es el punto de la pantalla."""
    from app.db import get_db, utcnow, enroll
    from app.lti import _cotejar

    with get_db() as db:
        cid = db.execute("INSERT INTO courses (name, active, created_at) VALUES ('M', 1, ?)",
                         (utcnow(),)).lastrowid
        eid = db.execute("INSERT INTO course_editions (course_id, etiqueta, active, created_at)"
                         " VALUES (?, '2026', 1, ?)", (cid, utcnow())).lastrowid
        uid = db.execute("INSERT INTO users (login, password_hash, full_name, role, active, created_at)"
                         " VALUES ('11111111', 'x', 'Ya, Está', 'student', 1, ?)", (utcnow(),)).lastrowid
        enroll(db, uid, eid)
        db.execute("INSERT INTO users (login, password_hash, full_name, role, active, created_at)"
                   " VALUES ('22222222', 'x', 'Solo, EnLidia', 'student', 1, ?)", (utcnow(),))
        otro = db.execute("SELECT id FROM users WHERE login='22222222'").fetchone()["id"]
        enroll(db, otro, eid)

    gente = [
        {"estudiante": True, "usuario": "11111111", "sourcedid": "", "nombre": "Ya Está",
         "email": "", "sub": "1", "docente": False, "activo": True},
        {"estudiante": True, "usuario": "33333333", "sourcedid": "", "nombre": "Nuevo Nuevo",
         "email": "n@e.com", "sub": "2", "docente": False, "activo": True},
        {"estudiante": True, "usuario": "", "sourcedid": "", "nombre": "Sin Documento",
         "email": "", "sub": "3", "docente": False, "activo": True},
        {"estudiante": False, "usuario": "prof", "sourcedid": "", "nombre": "El Docente",
         "email": "", "sub": "4", "docente": True, "activo": True},
    ]
    r = _cotejar(eid, gente)
    assert [c["usuario"]["login"] for c in r["cruzados"]] == ["11111111"]
    assert [m["usuario"] for m in r["faltan_alta"]] == ["33333333"]
    assert [m["nombre"] for m in r["sin_cruzar"]] == ["Sin Documento"]
    assert [s["usuario"]["login"] for s in r["sobrantes"]] == ["22222222"]
