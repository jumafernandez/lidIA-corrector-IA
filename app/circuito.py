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
              f"{edicion['materia']} {edicion['etiqueta']}", f"/admin/cursos/{eid}"),
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
        _paso("material", "Material", bool(con_material),
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
