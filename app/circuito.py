"""El camino de una cursada, de punta a punta, para no perderse en el medio.

Configurar una evaluación en LidIA tiene varios niveles —materia, cursada, programa,
instancia, material de corrección, activación— y se fueron sumando de a uno. Quien entra
por primera vez no tiene forma de saber en qué paso está ni cuál sigue.

Esto no explica nada ni da consejos: calcula de los datos qué está hecho y qué no, y deja
que la pantalla lo muestre como un mapa. Cuando todo está hecho, el mapa dice que está
todo hecho; no hay nada que callar ni que detectar sobre lo que la persona ya sabe.
"""

HECHO, ACTUAL, PENDIENTE = "hecho", "actual", "pendiente"


def _paso(clave, titulo, listo, detalle, url, voz=""):
    """`detalle` es el dato seco, para el globo del paso. `voz` es lo que dice Lidia."""
    return {"clave": clave, "titulo": titulo, "listo": listo, "detalle": detalle,
            "url": url, "voz": voz, "estado": HECHO if listo else PENDIENTE}


def circuito(db, edicion, assignment=None) -> list:
    """Los pasos de la cursada, en orden, con lo hecho marcado.

    Si se pasa una instancia, los pasos que dependen de ella miran esa; si no, miran
    el conjunto de la cursada.
    """
    eid = edicion["id"]

    n_doc = db.execute(
        "SELECT COUNT(*) n FROM course_teachers WHERE edition_id = ?", (eid,)).fetchone()["n"]
    n_est = db.execute(
        "SELECT COUNT(*) n FROM enrollments WHERE edition_id = ?", (eid,)).fetchone()["n"]
    instancias = db.execute(
        "SELECT * FROM assignments WHERE edition_id = ? ORDER BY id", (eid,)).fetchall()

    # El material y la activación son de una instancia concreta. Sin una elegida, se
    # mira la cursada entera: alcanza con que haya alguna lista para que el paso cuente.
    focos = [assignment] if assignment else instancias
    con_material = [a for a in focos if _material_completo(db, a)]
    activas = [a for a in focos if a["active"]]

    n_entregas = db.execute(
        "SELECT COUNT(*) n FROM submissions s JOIN assignments a ON a.id = s.assignment_id"
        " WHERE a.edition_id = ?" + (" AND a.id = ?" if assignment else ""),
        (eid, assignment["id"]) if assignment else (eid,)).fetchone()["n"]
    n_pendientes = db.execute(
        "SELECT COUNT(*) n FROM submissions s JOIN assignments a ON a.id = s.assignment_id"
        " WHERE a.edition_id = ? AND s.kind = 'final' AND s.status = 'pendiente'"
        + (" AND a.id = ?" if assignment else ""),
        (eid, assignment["id"]) if assignment else (eid,)).fetchone()["n"]
    n_firmadas = db.execute(
        "SELECT COUNT(*) n FROM submissions s JOIN assignments a ON a.id = s.assignment_id"
        " WHERE a.edition_id = ? AND s.status = 'aprobada'"
        + (" AND a.id = ?" if assignment else ""),
        (eid, assignment["id"]) if assignment else (eid,)).fetchone()["n"]

    aid = assignment["id"] if assignment else (instancias[0]["id"] if instancias else None)
    ficha = f"/admin/instancias/{aid}" if aid else f"/admin/cursos/{eid}/instancias/nueva"

    pasos = [
        _paso("cursada", "Cursada", True,
              edicion['nombre'], f"/admin/cursos/{eid}"),
        _paso("programa", "Programa", bool((edicion["programa"] or "").strip()),
              "El programa aprobado de esta cursada",
              f"/admin/cursos/{eid}/programa",
              "Todavía no me cargaste el programa. Con él puedo decirle a alguien «esto lo "
              "vimos en la unidad 4» en vez de recomendarle bibliografía al azar."),
        _paso("docentes", "Docentes", n_doc > 0,
              f"{n_doc} asignado{'s' if n_doc != 1 else ''}", f"/admin/cursos/{eid}",
              "Esta cursada no tiene docentes asignados, así que nadie puede firmar "
              "correcciones todavía."),
        _paso("estudiantes", "Estudiantes", n_est > 0,
              f"{n_est} inscripto{'s' if n_est != 1 else ''}",
              f"/admin/estudiantes?curso={eid}",
              "Falta inscribir estudiantes. Podés cargarlos de a uno, importar un listado, "
              "o traerlos del campus si la cursada está vinculada."),
        _paso("instancia", "Instancia", bool(instancias),
              (assignment["name"] if assignment
               else f"{len(instancias)} creada{'s' if len(instancias) != 1 else ''}"),
              ficha,
              "Todavía no hay ninguna instancia de evaluación: un TP, un parcial, un "
              "trabajo final. Es lo que el estudiantado va a entregar."),
        _paso("material", "Consigna", bool(con_material),
              _falta_material(db, assignment) if assignment else
              f"{len(con_material)} de {len(instancias)} con material completo", ficha,
              ((_falta_material(db, assignment) + ". Sin eso no sé contra qué corregir.")
               if assignment else
               "Hay instancias sin el material de corrección completo. Sin consigna y "
               "rúbrica no sé contra qué corregir.")),
        _paso("activa", "Activa", bool(activas),
              "Visible para el estudiantado" if activas else "Todavía en borrador", ficha,
              "Está en borrador, así que el estudiantado todavía no la ve. Cuando el "
              "material esté completo, marcala como activa."),
        _paso("entregas", "Entregas", n_entregas > 0,
              f"{n_entregas} recibida{'s' if n_entregas != 1 else ''}", "/admin/entregas",
              "Ya está todo listo de tu lado. Ahora es cuestión de que entreguen: cuando "
              "llegue la primera, la vas a ver acá."),
        _paso("corregidas", "Corregidas", n_firmadas > 0,
              (f"{n_pendientes} esperando tu firma" if n_pendientes
               else f"{n_firmadas} firmada{'s' if n_firmadas != 1 else ''}"),
              "/admin/entregas",
              (f"Hay {n_pendientes} entrega{'s' if n_pendientes != 1 else ''} esperando tu "
               "firma. Yo ya dejé una propuesta de devolución para cada una."
               if n_pendientes else
               "Todavía no firmaste ninguna corrección en esta cursada.")),
    ]

    # El primer paso sin hacer es dónde está parada la persona: lo que sigue.
    for p in pasos:
        if not p["listo"]:
            p["estado"] = ACTUAL
            break
    return pasos


def _material_completo(db, a) -> bool:
    """¿Tiene lo que hace falta para poder activarse? Mismo criterio que la activación."""
    if not (a["consigna"] or "").strip():
        return False
    if a["tipo"] == "abierto":
        return bool((a["rubrica"] or "").strip())
    items = db.execute(
        "SELECT respuesta FROM assignment_items WHERE assignment_id = ?", (a["id"],)).fetchall()
    if items:
        return all((i["respuesta"] or "").strip() for i in items)
    return bool((a["respuestas"] or "").strip())


def _falta_material(db, a) -> str:
    """Qué falta exactamente, para poder decirlo en lugar de decir «incompleto»."""
    if not (a["consigna"] or "").strip():
        return "Falta la consigna"
    if a["tipo"] == "abierto":
        return "Listo" if (a["rubrica"] or "").strip() else "Falta la rúbrica"
    items = db.execute(
        "SELECT orden, respuesta FROM assignment_items WHERE assignment_id = ? ORDER BY orden",
        (a["id"],)).fetchall()
    if not items:
        return "Listo" if (a["respuestas"] or "").strip() else "Faltan las preguntas"
    sin = [str(i["orden"]) for i in items if not (i["respuesta"] or "").strip()]
    if not sin:
        return f"{len(items)} pregunta{'s' if len(items) != 1 else ''} con su respuesta"
    cual = "la opción correcta" if a["tipo"] == "choice" else "la respuesta esperada"
    return f"Falta {cual} de la pregunta {', '.join(sin)}"


def resumen(pasos: list) -> dict:
    """Para el encabezado: cuánto hay hecho y cuál es el próximo paso."""
    hechos = [p for p in pasos if p["listo"]]
    actual = next((p for p in pasos if p["estado"] == ACTUAL), None)
    return {"hechos": len(hechos), "total": len(pasos), "siguiente": actual,
            "completo": actual is None}


# --------------------------------------------------------------- consejo por pantalla
#
# El circuito de arriba vive en la ficha de una cursada, donde hay una sola a la vista.
# En los listados no hay una cursada elegida, pero la persona igual está parada en algún
# punto del camino, y cada pantalla es responsable de un tramo distinto. Esto busca, entre
# las cursadas de quien mira, el primer paso pendiente que le corresponde a esa pantalla.
#
# No hay mensajes nuevos: se reusa la `voz` que cada paso ya tiene escrita. Si no falta
# nada de lo que esa pantalla cubre, devuelve None y Lidia no aparece: el silencio es la
# señal de que está todo hecho.

TRAMOS = {
    "cursos": ("programa", "docentes", "estudiantes", "instancia", "material", "activa"),
    "materias": ("programa", "instancia"),
    "instancias": ("instancia", "material", "activa"),
    "estudiantes": ("estudiantes",),
    "entregas": ("entregas", "corregidas"),
}

# Tope de cursadas que se recorren. Coordinación puede tener decenas y cada una cuesta
# varias consultas; el consejo es una ayuda, no vale hacerle esperar el listado por él.
TOPE_REVISADAS = 15


def consejo(db, cursadas, pantalla: str):
    """El primer paso pendiente que le toca a esta pantalla, o None si no hay ninguno.

    `cursadas` son las ediciones visibles para quien mira, ya filtradas por permisos.
    Devuelve {"voz", "url", "titulo", "cursada"} listo para el globo.
    """
    claves = TRAMOS.get(pantalla)
    if not claves:
        return None
    for ed in list(cursadas)[:TOPE_REVISADAS]:
        # Solo el PRIMER paso pendiente, no cualquiera del tramo: el circuito es ordenado,
        # y hablar de las entregas mientras falta el material haría decir «ya está todo
        # listo de tu lado» a una cursada que todavía no puede corregir nada.
        paso = next((p for p in circuito(db, ed) if not p["listo"]), None)
        if paso and paso["clave"] in claves:
            return {"voz": paso["voz"], "url": paso["url"], "titulo": paso["titulo"],
                    "cursada": ed['nombre']}
    return None


def consejo_sin_cursadas(puede_crear: bool) -> dict:
    """Qué decir cuando la persona todavía no tiene ninguna cursada de la que hablar."""
    if puede_crear:
        return {"voz": "Todavía no tenés ninguna cursada. Creá la primera y te voy marcando "
                       "qué falta en cada paso, hasta que puedas recibir entregas.",
                "url": "/admin/cursos/nuevo", "titulo": "Nueva cursada", "cursada": ""}
    return {"voz": "Todavía no te asignaron ninguna cursada. En cuanto la coordinación te sume "
                   "a una, acá vas a ver qué falta configurar.",
            "url": "", "titulo": "", "cursada": ""}


# ------------------------------------------------------------------- cupos por tipo
#
# Cuántas versiones mejorables admite una instancia antes de la entrega definitiva, y
# cuántas repreguntas se pueden hacer sobre cada devolución.
#
# El circuito es uno solo para las tres modalidades —se entrega, Lidia corrige, hay una
# entrega definitiva— y lo único que cambia entre ellas es este par de números. Un trabajo
# se rehace hasta que da; un parcial se rinde una vez, y su única entrega ES la definitiva.
#
# Está acá, en un renglón por tipo, a propósito. Que un examen no admita versiones previas
# es una decisión sobre cómo se evalúa, no una propiedad del software: el día que una
# cátedra quiera un domiciliario escrito con dos devoluciones antes de entregar, se cambia
# el máximo de ese renglón y no hay una sola línea más que tocar en ningún lado.
CUPOS = {
    #            practicas: (mínimo, máximo, por defecto)
    "abierto": {"practicas": (0, 10, 3), "preguntas": (0, 10, 3)},
    "escrito": {"practicas": (0, 0, 0), "preguntas": (0, 0, 0)},
    "choice":  {"practicas": (0, 0, 0), "preguntas": (0, 0, 0)},
}
CAMPOS = ("practicas", "preguntas")


def cupo(tipo: str, campo: str) -> tuple:
    """(mínimo, máximo, por defecto) de un cupo para un tipo de evaluación."""
    return CUPOS.get(tipo, CUPOS["abierto"])[campo]


def fijo(tipo: str, campo: str) -> bool:
    """¿Lo decide el tipo de evaluación en vez del equipo docente?"""
    minimo, maximo, _ = cupo(tipo, campo)
    return minimo == maximo


def ajustar(tipo: str, campo: str, valor) -> int:
    """Lo que corresponde guardar: lo pedido, dentro de lo que el tipo admite.

    Se aplica siempre del lado del servidor. Que el formulario muestre el campo bloqueado
    no alcanza: un campo bloqueado no es una restricción, es una cortesía visual.
    """
    minimo, maximo, defecto = cupo(tipo, campo)
    try:
        n = int(valor)
    except (TypeError, ValueError):
        n = defecto
    return max(minimo, min(maximo, n))


def cupos_de(assignment) -> dict:
    """Los dos cupos de una instancia, ya ajustados a su tipo, para pintar la ficha."""
    tipo = assignment["tipo"]
    guardado = {"practicas": assignment["max_practicas"], "preguntas": assignment["max_preguntas"]}
    salida = {}
    for campo in CAMPOS:
        minimo, maximo, _ = cupo(tipo, campo)
        salida[campo] = {"valor": ajustar(tipo, campo, guardado[campo]),
                         "min": minimo, "max": maximo, "fijo": minimo == maximo}
    return salida


def defectos(tipo: str) -> dict:
    """Los cupos con los que nace una instancia de este tipo."""
    return {campo: cupo(tipo, campo)[2] for campo in CAMPOS}
