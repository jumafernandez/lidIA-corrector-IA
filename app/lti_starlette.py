"""Adaptador de pylti1p3 para Starlette/FastAPI.

La librería está escrita para Flask y Django: define clases abstractas para pedir el
request, las cookies y la redirección, y cada framework aporta las suyas. Esto es eso.

El punto delicado está en `CookieServiceStarlette`. El lanzamiento LTI llega como POST
desde otro sitio (Moodle), y en esa navegación el navegador NO manda cookies `SameSite=Lax`.
Si la cookie del `state` sale con los atributos por defecto, el lanzamiento muere con
«State not found» y uno pierde la noche mirando el state, que está perfecto. Por eso acá
se fuerza `Secure; SameSite=None` siempre, sin mirar el esquema: Chrome acepta `Secure`
sobre http cuando el host es loopback, así que también funciona en el laboratorio local.
"""
import typing as t

from pylti1p3.cookie import CookieService
from pylti1p3.message_launch import MessageLaunch
from pylti1p3.oidc_login import OIDCLogin
from pylti1p3.redirect import Redirect
from pylti1p3.request import Request
from pylti1p3.session import SessionService
from starlette.requests import Request as PedidoHTTP
from starlette.responses import HTMLResponse, RedirectResponse, Response


class PedidoStarlette(Request):
    """El pedido HTTP como lo espera pylti1p3.

    El formulario se pasa ya resuelto porque leerlo es asíncrono y la librería es
    síncrona: quien crea esto tiene que haber hecho `await request.form()` antes.
    """

    def __init__(self, request: PedidoHTTP, form: t.Optional[t.Mapping] = None):
        super().__init__()
        self._request = request
        self._form = form
        self._session: dict = {}

    @property
    def session(self) -> dict:
        return self._session

    def is_secure(self) -> bool:
        return self._request.url.scheme == "https"

    def get_param(self, key: str):
        if self._request.method == "GET":
            return self._request.query_params.get(key)
        if self._form is None:
            raise RuntimeError("Falta el formulario: hacer 'await request.form()' antes.")
        return self._form.get(key)

    def get_cookie(self, key: str) -> t.Optional[str]:
        return self._request.cookies.get(key)


class CookieServiceStarlette(CookieService):
    """Cookies del apretón de manos, siempre aptas para POST entre sitios."""

    def __init__(self, request: PedidoStarlette):
        self._request = request
        self._pendientes: t.Dict[str, dict] = {}

    def _clave(self, name: str) -> str:
        return f"{self._cookie_prefix}-{name}"

    def get_cookie(self, name: str) -> t.Optional[str]:
        return self._request.get_cookie(self._clave(name))

    def set_cookie(self, name: str, value: str, exp: int = 3600):
        self._pendientes[self._clave(name)] = {"value": value, "exp": exp}

    def update_response(self, response: Response) -> Response:
        for clave, datos in self._pendientes.items():
            # secure=True y samesite="none" SIEMPRE. Ver el docstring del módulo:
            # con "lax" la cookie no viaja en el POST de vuelta y el lanzamiento falla.
            response.set_cookie(
                key=clave, value=str(datos["value"]), max_age=datos["exp"],
                path="/", httponly=True, secure=True, samesite="none",
            )
        return response


class RedireccionStarlette(Redirect[Response]):
    def __init__(self, destino: str, cookies: t.Optional[CookieServiceStarlette] = None):
        super().__init__()
        self._destino = destino
        self._cookies = cookies

    def do_redirect(self):
        return self._con_cookies(RedirectResponse(self._destino, status_code=302))

    def do_js_redirect(self):
        return self._con_cookies(HTMLResponse(
            f'<script>window.location={self._destino!r};</script>'))

    def set_redirect_url(self, location: str):
        self._destino = location

    def get_redirect_url(self) -> str:
        return self._destino

    def _con_cookies(self, response: Response) -> Response:
        return self._cookies.update_response(response) if self._cookies else response


class SesionStarlette(SessionService):
    """Sin cambios: el almacenamiento real lo pone AlmacenSqlite."""


class LoginOIDC(OIDCLogin):
    def __init__(self, request, tool_config, session_service=None, cookie_service=None,
                 launch_data_storage=None):
        cookie_service = cookie_service or CookieServiceStarlette(request)
        session_service = session_service or SesionStarlette(request)
        super().__init__(request, tool_config, session_service, cookie_service,
                         launch_data_storage)

    def get_redirect(self, url: str):
        return RedireccionStarlette(url, self._cookie_service)

    def get_response(self, html: str):
        return self._cookie_service.update_response(HTMLResponse(html))


class Lanzamiento(MessageLaunch):
    def __init__(self, request, tool_config, session_service=None, cookie_service=None,
                 launch_data_storage=None, requests_session=None):
        cookie_service = cookie_service or CookieServiceStarlette(request)
        session_service = session_service or SesionStarlette(request)
        super().__init__(request, tool_config, session_service, cookie_service,
                         launch_data_storage, requests_session)

    def _get_request_param(self, key: str):
        return self._request.get_param(key)
