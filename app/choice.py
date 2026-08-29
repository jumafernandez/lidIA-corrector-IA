"""Corrección de un multiple choice: la nota la calcula el software.

Comparar la letra elegida con la clave y sumar puntajes es aritmética. Pedírselo a un
modelo de lenguaje es aceptar que a veces se equivoque en una cuenta, y equivocarse ahí no
se nota: la devolución igual suena bien y la nota queda mal. Acá el resultado sale de
código, es reproducible, y si alguien reclama se puede mostrar de dónde salió cada punto.

Lo que sí hace el modelo es lo que un modelo hace bien y una cuenta no: explicarle a quien
se equivocó por qué la opción que eligió no va y por qué la correcta sí. Esa parte vive en
`llm.explicar_errores`, y recibe solo las preguntas erradas.
"""
import re

# Cómo puede venir escrita una respuesta. La consigna pide «1-b», pero alguien escribe
# «1) b», «Pregunta 3: c» o «2. B», y ninguna de esas es un error de contenido.
RESPUESTA = re.compile(
    r"^\s*(?:pregunta\s*)?(\d{1,3})\s*[-.):=\s]\s*\(?\s*([a-hA-H])\s*\)?\s*$",
    re.IGNORECASE,
)
# La misma idea pero suelta en el medio de una oración: «la 3 es la c».
RESPUESTA_SUELTA = re.compile(
    r"(?:pregunta\s*)?\b(\d{1,3})\b[^\w\n]{0,12}(?:es\s+la\s+|es\s+|=\s*)?\(?([a-hA-H])\)?(?=\W|$)",
    re.IGNORECASE,
)


def leer_respuestas(texto: str, cantidad: int) -> dict:
    """Qué letra eligió en cada pregunta. {numero: letra} y ausencia = sin responder.

    Primero se buscan renglones con una respuesta por línea, que es el formato que pide la
    consigna. Solo si así no aparecen todas se barre el texto suelto, porque ahí cualquier
    «capítulo 2 opción a» se puede confundir con una respuesta.
    """
    elegidas: dict = {}
    for linea in (texto or "").splitlines():
        m = RESPUESTA.match(linea)
        if m:
            n = int(m.group(1))
            if 1 <= n <= cantidad:
                elegidas[n] = m.group(2).lower()

    if len(elegidas) < cantidad:
        for m in RESPUESTA_SUELTA.finditer(texto or ""):
            n = int(m.group(1))
            if 1 <= n <= cantidad and n not in elegidas:
                elegidas[n] = m.group(2).lower()
    return elegidas


def _letra(indice: int) -> str:
    return chr(ord("a") + indice)


def corregir_marcadas(items: list, elegidas: dict) -> dict:
    """Corrige a partir de las respuestas ya elegidas. Es el camino normal.

    Cuando el examen se responde marcando opciones, la respuesta llega estructurada y no
    hay nada que leer: ni expresión regular, ni llamada a un modelo, ni la posibilidad de
    entender mal lo que alguien quiso decir. Leer texto queda solo para el examen en
    papel, donde lo único que hay es la transcripción de la hoja.
    """
    return _armar(items, elegidas)


def corregir(items: list, texto: str) -> dict:
    """Corrige el examen entero. Devuelve el detalle por pregunta y la nota.

    `items` son las preguntas con su clave y su puntaje. La nota es la proporción de
    puntaje obtenido, en escala 0–10 y redondeada a dos decimales: es lo que se propone al
    docente, que sigue siendo quien firma.
    """
    return _armar(items, leer_respuestas(texto, len(items)))


def _armar(items: list, elegidas: dict) -> dict:
    """El resultado, venga la respuesta marcada o leída de un texto."""
    detalle, obtenido, maximo = [], 0.0, 0.0
    for i, it in enumerate(items, start=1):
        puntaje = float(it["puntaje"] or 1)
        maximo += puntaje
        correcta = (it["respuesta"] or "").strip().lower()[:1]
        elegida = elegidas.get(i)
        acerto = bool(elegida and correcta and elegida == correcta)
        if acerto:
            obtenido += puntaje
        opciones = _opciones(it["opciones"])
        detalle.append({
            "n": i,
            "enunciado": it["enunciado"],
            "opciones": opciones,
            "elegida": elegida,
            "correcta": correcta,
            "acerto": acerto,
            "sin_responder": elegida is None,
            "puntaje": puntaje,
            "texto_elegida": _texto_opcion(opciones, elegida),
            "texto_correcta": _texto_opcion(opciones, correcta),
        })

    nota = round(10 * obtenido / maximo, 2) if maximo else 0.0
    return {"detalle": detalle, "obtenido": obtenido, "maximo": maximo, "nota": nota,
            "aciertos": sum(1 for d in detalle if d["acerto"]),
            "sin_responder": sum(1 for d in detalle if d["sin_responder"]),
            "total": len(items)}


def _opciones(crudo) -> list:
    """Las opciones, vengan como texto de la base o ya separadas desde la configuración."""
    if isinstance(crudo, (list, tuple)):
        return [str(o).strip() for o in crudo if str(o).strip()]
    return [o.strip() for o in (crudo or "").splitlines() if o.strip()]


def _texto_opcion(opciones: list, letra: str | None) -> str:
    if not letra:
        return ""
    i = ord(letra.lower()) - ord("a")
    return opciones[i] if 0 <= i < len(opciones) else ""


def tabla_markdown(resultado: dict) -> str:
    """El resultado como lo ve quien lo recibe: qué se leyó, qué era, cuánto sumó.

    Se muestra lo que el sistema entendió que respondió, no solo si acertó: si leyó mal
    una respuesta, la persona lo ve y puede reclamar con el detalle a la vista.
    """
    filas = ["| # | Tu respuesta | Correcta | Puntos |", "|---|---|---|---|"]
    for d in resultado["detalle"]:
        tuya = "— (sin responder)" if d["sin_responder"] else f"**{d['elegida']})**"
        marca = "✅" if d["acerto"] else "❌"
        puntos = f"{_num(d['puntaje'])} / {_num(d['puntaje'])}" if d["acerto"] else f"0 / {_num(d['puntaje'])}"
        filas.append(f"| {d['n']} | {tuya} {marca} | {d['correcta']}) | {puntos} |")
    r = "\n".join(filas)
    r += (f"\n\n**Total: {_num(resultado['obtenido'])} de {_num(resultado['maximo'])} puntos** "
          f"({resultado['aciertos']} de {resultado['total']} correctas)."
          f"  \nCalificación sugerida: **{_num(resultado['nota'])}/10**. "
          "La calificación oficial la define el equipo docente.")
    if resultado["sin_responder"]:
        n = resultado["sin_responder"]
        r += (f"\n\n> Quedó {n} pregunta sin respuesta legible. " if n == 1 else
              f"\n\n> Quedaron {n} preguntas sin respuesta legible. ")
        r += ("Si respondiste y no aparece acá, avisale al equipo docente: se corrige a mano.")
    return r


def _num(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else f"{x:.2f}".rstrip("0").rstrip(".")
