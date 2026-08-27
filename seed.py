"""Crea el usuario de coordinación y, opcionalmente, datos de demostración.

Uso normal (instalación nueva):

    LIDIA_ADMIN_LOGIN=30111222 LIDIA_ADMIN_NAME="Apellido, Nombre" python seed.py

La contraseña se genera al azar y se imprime una sola vez. Con --demo se agregan además
las cuatro materias de la Diplomatura de Posgrado en Desarrollo de Soluciones de IA
Generativa en la Nube (LICDIA, UNLu) con su edición 2026, sus docentes y tres estudiantes
de prueba, para mostrar el sistema sin cargar datos reales de estudiantes.
"""
import os
import sys

from app import auth
from app.db import enroll, get_db, init_db, utcnow

ADMIN_LOGIN = os.environ.get("LIDIA_ADMIN_LOGIN", "admin")
ADMIN_NAME = os.environ.get("LIDIA_ADMIN_NAME", "Coordinación")
ADMIN_EMAIL = os.environ.get("LIDIA_ADMIN_EMAIL", "")

# Las cuatro materias de la diplomatura, en el orden del plan de estudios.
# La clave corta es la que usan DOCENTES_DEMO e INSTANCIAS_DEMO para referirse a cada una.
MATERIAS_DEMO = [
    ("intro", "Introducción a la Inteligencia Artificial"),
    ("infra", "Infraestructura Tecnológica para Inteligencia Artificial"),
    ("dl", "Aprendizaje Profundo y Redes Neuronales"),
    ("genia", "Inteligencia Artificial Generativa"),
]
EDICION_DEMO = "2026"

# La cursada 2026 de Introducción a la IA va del 8 de septiembre al 1 de octubre.
FECHAS_DEMO = {"intro": ("2026-09-08", "2026-10-01")}

# El primer docente es quien corre el seed: se usa su cuenta de coordinación en lugar de
# crear una segunda cuenta para la misma persona. El resto se crean como docentes.
DOCENTES_DEMO = [
    (None, None, ("intro", "dl", "genia")),
    ("dpetrocelli", "Petrocelli, David", ("infra", "genia")),
]

ESTUDIANTES_DEMO = [
    ("30111222", "Gómez, María", "mgomez@ejemplo.com"),
    ("28333444", "Pérez, Juan", "jperez@ejemplo.com"),
    ("33555666", "Suárez, Ana", ""),
]

PROGRAMA_INTRO = """\
## Introducción a la Inteligencia Artificial

Curso de posgrado de la Diplomatura de Posgrado en Desarrollo de Soluciones de Inteligencia
Artificial Generativa en la Nube (plan DP01.01). Modalidad a distancia, 40 horas (20 teóricas y
20 prácticas; 16 sincrónicas y 24 asincrónicas).

**Docente responsable:** Fernández, Juan Manuel.
**Equipo docente:** Gasch, Diego Emanuel.

### Objetivo general
Promover una comprensión profunda y crítica de los fundamentos de la Inteligencia Artificial, que
permita analizar, contrastar y aplicar diferentes enfoques y técnicas, y reflexionar sobre sus
implicancias éticas y sociales.

### Contenidos

**Unidad 1. Fundamentos de la Inteligencia Artificial.** Enfoques históricos: simbólico, estadístico
y conexionista. Principales aplicaciones en distintos sectores (salud, industria, educación, arte).
Tendencias actuales y problemáticas emergentes.

**Unidad 2. Aprendizaje Automático.** Datos de entrenamiento, validación y prueba. Algoritmos
supervisados: regresión lineal y logística, árboles de decisión, k-NN. Algoritmos no supervisados:
clustering (k-means, jerárquico). Introducción a métricas de desempeño.

**Unidad 3. Introducción a Redes Neuronales.** Límites de los modelos lineales. El perceptrón:
estructura, función de activación y entrenamiento. Redes feed-forward de una y varias capas.
Aplicaciones iniciales en clasificación y regresión. Transición hacia arquitecturas profundas.

**Unidad 4. Procesamiento de Lenguaje Natural y Visión por Computadora.** NLP: representación de
texto (bag of words, TF-IDF, embeddings genéricos y noción inicial de embeddings contextuales),
tokenización y procesamiento. Visión por computadora: reconocimiento de patrones, clasificación de
imágenes. Casos de uso actuales en ambos campos.

**Unidad 5. Ética y Responsabilidad en IA.** Sesgos algorítmicos y discriminación. Privacidad y
protección de datos. Marcos regulatorios emergentes. Impactos sociales y culturales. Transparencia
y sostenibilidad. Responsabilidad legal y accountability. Deepfakes y desinformación. Inclusión y
accesibilidad.

### Metodología
Encuentros sincrónicos de exposición conceptual y demostración de implementaciones en Python
(Google Colab o Jupyter). Actividades asincrónicas de experimentación autónoma sobre consignas
guiadas, ejercicios prácticos y foros de reflexión.

### Aprobación
Cursada: 70% de asistencia a los encuentros sincrónicos y presentación de un caso de estudio
preliminar según el template del equipo docente (datos a utilizar, objetivos del modelo, tipo de
modelo y técnicas metodológicas). Aprobación final: entrega del trabajo práctico integrador
completo, que desarrolla el caso de estudio planteado.

### Bibliografía
- Russell, S., & Norvig, P. (2021). *Artificial Intelligence: A Modern Approach* (4a ed.). Pearson.
- Géron, A. (2022). *Hands-on Machine Learning with Scikit-Learn, Keras, and TensorFlow*. O'Reilly.
- Mitchell, T. (1997). *Machine Learning*. McGraw-Hill.
- Jurafsky, D., & Martin, J. H. (2025). *Speech and Language Processing* (3a ed., borrador).
- Alammar, J., & Grootendorst, M. (2024). *Hands-on Large Language Models*. O'Reilly.
- Vaswani, A. et al. (2017). Attention is all you need. *NeurIPS 30*.
- Jobin, A., Ienca, M., & Vayena, E. (2019). The global landscape of AI ethics guidelines.
  *Nature Machine Intelligence, 1*(9), 389-399.
"""

# Instancias de evaluación por materia. La de Introducción a la IA es la real de la
# cursada 2026: el programa la plantea en dos tiempos (un caso de estudio preliminar y
# el trabajo integrador completo), que en este modelo son dos instancias de la misma
# cursada, cada una con su consigna y su rúbrica.
INSTANCIAS_DEMO = {
    "intro": [
        dict(
            nombre="Propuesta de Trabajo Final",
            tipo="abierto",
            max_integrantes=2,
            consigna=(
                "Antes de empezar el trabajo final hay que acordar de qué se trata. Esta entrega es "
                "esa propuesta: un documento breve (dos o tres páginas) que delimite el caso de "
                "estudio y permita al equipo docente confirmar que es viable dentro del alcance del "
                "curso.\n\n"
                "La propuesta tiene que definir cuatro cosas:\n\n"
                "1. **El problema.** Qué necesidad concreta se quiere resolver y por qué es "
                "relevante. Delimitado: no «analizar datos de salud» sino una pregunta que un modelo "
                "pueda responder.\n"
                "2. **Los datos.** Qué conjunto se va a usar, de dónde sale y con qué licencia. Tiene "
                "que provenir de fuentes abiertas y su uso tiene que ser éticamente admisible, en los "
                "términos discutidos en la unidad 5. Indicá cantidad de registros, variables "
                "disponibles y qué es lo que se quiere predecir o descubrir.\n"
                "3. **El tipo de modelo.** Qué se va a entrenar y por qué ese enfoque y no otro: "
                "aprendizaje supervisado o no supervisado, y dentro de eso qué familia de algoritmos "
                "(unidades 2 y 3), o técnicas de NLP o visión por computadora (unidad 4).\n"
                "4. **Cómo se va a evaluar.** Qué métricas se van a usar y por qué son las adecuadas "
                "para este problema, y cómo se van a separar los datos de entrenamiento, validación "
                "y prueba.\n\n"
                "El trabajo puede ser individual o en grupos de hasta dos personas. El tema se acuerda "
                "con el equipo docente: esta propuesta es el instrumento de ese acuerdo, así que "
                "conviene entregarla con tiempo para poder corregir el rumbo."
            ),
            rubrica=(
                "1. Delimitación del problema: la pregunta es concreta, relevante y del tamaño de un "
                "trabajo de curso. Se entiende qué se quiere resolver y para quién.\n"
                "2. Datos: el conjunto existe, es accesible y es de fuente abierta; se declara su "
                "origen y su licencia. Se describe qué contiene (registros, variables) y se explicita "
                "qué se predice o se agrupa. Se contemplan las condiciones éticas de uso.\n"
                "3. Elección del modelo: el tipo de modelo es adecuado al problema y a los datos "
                "declarados, y la elección está justificada frente a alternativas razonables.\n"
                "4. Plan de evaluación: las métricas propuestas corresponden al tipo de problema y la "
                "partición de los datos es correcta.\n"
                "5. Viabilidad: el alcance es realizable con los datos, las herramientas y el tiempo "
                "disponibles.\n"
                "6. Claridad del documento: organización, precisión terminológica y uso de fuentes."
            ),
        ),
        dict(
            nombre="Trabajo Final Integrador",
            tipo="abierto",
            max_integrantes=2,
            consigna=(
                "El trabajo final desarrolla el caso de estudio aprobado en la propuesta: diseño, "
                "implementación y evaluación de un modelo de inteligencia artificial sobre un conjunto "
                "de datos del mundo real, de fuente abierta y con condiciones éticas de uso.\n\n"
                "La entrega incluye el informe y el código (notebook o repositorio) que permita "
                "reproducir lo que el informe afirma. El informe tiene que dar cuenta de:\n\n"
                "1. **Problema y datos.** El problema, ya delimitado, y el conjunto de datos "
                "efectivamente usado: origen, licencia, tamaño, variables y el preprocesamiento que "
                "hubo que hacer (valores faltantes, codificación, escalado, partición).\n"
                "2. **Modelo.** Qué se entrenó y por qué, cómo se eligieron los hiperparámetros y qué "
                "alternativas se probaron. Si el trabajo compara enfoques, en qué condiciones se "
                "compararon.\n"
                "3. **Evaluación.** Los resultados con las métricas adecuadas al problema, medidos "
                "sobre datos que el modelo no vio. Interesa más el análisis de los resultados que el "
                "número: qué casos falla, si hay clases donde anda peor, si el desempeño alcanza para "
                "el uso propuesto.\n"
                "4. **Reflexión crítica.** Alcances y limitaciones de la solución, y sus implicancias "
                "éticas: sesgos posibles en los datos o en el modelo, a quién podría perjudicar un "
                "error, qué haría falta antes de ponerlo a funcionar de verdad.\n\n"
                "No se espera un resultado espectacular: se espera un trabajo honesto, donde las "
                "decisiones estén fundamentadas y las conclusiones se sostengan en lo que se midió. "
                "Un modelo que anda mal, bien analizado, vale más que uno que anda bien y no se "
                "entiende por qué."
            ),
            rubrica=(
                "1. Coherencia entre diseño e implementación: lo implementado corresponde a lo "
                "propuesto, y los desvíos respecto de la propuesta están explicados.\n"
                "2. Calidad técnica del modelo: el preprocesamiento es correcto, la partición de los "
                "datos evita fuga de información, el entrenamiento y la selección de hiperparámetros "
                "están fundamentados.\n"
                "3. Pertinencia de las métricas: las métricas corresponden al tipo de problema y a la "
                "distribución de los datos; los resultados se miden sobre datos no vistos.\n"
                "4. Análisis de resultados: se interpreta lo que los números significan, se identifican "
                "los casos en que el modelo falla y se discute si el desempeño alcanza para el uso "
                "propuesto.\n"
                "5. Reflexión crítica: se discuten alcances, limitaciones e implicancias éticas de la "
                "solución con la profundidad trabajada en la unidad 5.\n"
                "6. Comunicación: el informe es claro y está organizado; el código permite reproducir "
                "lo que el informe afirma; las fuentes están citadas."
            ),
        ),
    ],
    "infra": [dict(
        nombre="TP: arquitectura de despliegue", tipo="abierto",
        consigna=(
            "Diseñá la infraestructura para poner en producción una solución de IA. La entrega "
            "describe los servicios elegidos, el flujo de datos, los costos estimados y los "
            "compromisos que asumís en latencia, disponibilidad y privacidad de los datos."
        ),
        rubrica=(
            "1. Arquitectura: los componentes elegidos resuelven el problema y están justificados.\n"
            "2. Compromisos: se explicitan los trade-offs de costo, latencia y disponibilidad.\n"
            "3. Datos: el tratamiento de los datos es coherente con su sensibilidad.\n"
            "4. Costos: la estimación es realista y está fundamentada.\n"
            "5. Calidad del informe: claridad del diagrama y de la exposición."
        ),
    )],
    "dl": [dict(
        nombre="Parcial: fundamentos de redes neuronales", tipo="escrito",
        consigna=(
            "Parcial del módulo de Aprendizaje Profundo. Respondé las preguntas de forma completa y "
            "justificada. Tiempo: 2 horas, sin material."
        ),
        rubrica="",
    )],
    "genia": [dict(
        nombre="Trabajo Final Integrador", tipo="abierto", max_integrantes=2,
        consigna=(
            "Desarrollá una solución de IA generativa aplicada a un problema real, integrando los "
            "cuatro módulos de la diplomatura: fundamentos, infraestructura, aprendizaje profundo e "
            "IA generativa. La entrega es un informe que describa el problema, la arquitectura de la "
            "solución, la implementación (modelo, despliegue, infraestructura) y la evaluación de "
            "resultados."
        ),
        rubrica=(
            "1. Problema y justificación: relevancia del problema elegido, claridad de objetivos y "
            "alcance.\n"
            "2. Arquitectura de la solución: diseño de la solución generativa (modelo, prompts, RAG o "
            "ajuste fino si aplica), con decisiones fundamentadas.\n"
            "3. Implementación e infraestructura: uso de infraestructura en la nube, reproducibilidad "
            "y buenas prácticas.\n"
            "4. Evaluación de resultados: métricas o criterios de evaluación y análisis crítico de "
            "limitaciones.\n"
            "5. Calidad del informe: organización, claridad, terminología, citas y referencias."
        ),
    )],
}

# Preguntas del parcial de Aprendizaje Profundo, con su respuesta esperada y su puntaje.
PREGUNTAS_DL = [
    ("Explicá qué problema resuelve la retropropagación y por qué fue determinante para el entrenamiento "
     "de redes profundas.",
     "Calcula el gradiente de la función de pérdida respecto de cada peso aplicando la regla de la cadena "
     "hacia atrás y reutilizando los cálculos de cada capa. Sin eso, estimar el gradiente de millones de "
     "parámetros sería computacionalmente inviable.", 3),
    ("¿Qué es el sobreajuste y con qué técnicas se lo combate en una red neuronal? Explicá el mecanismo "
     "de una de ellas.",
     "El modelo memoriza el conjunto de entrenamiento y pierde capacidad de generalizar: el error de "
     "entrenamiento baja mientras el de validación sube. Se combate con regularización L1/L2, abandono "
     "(dropout), detención temprana, aumento de datos o normalización por lotes. El abandono apaga al azar "
     "una fracción de las neuronas en cada paso, de modo que la red no puede depender de ninguna en "
     "particular.", 4),
    ("¿Por qué una red convolucional es más adecuada que una totalmente conectada para procesar imágenes?",
     "Explota la estructura espacial: los filtros son locales y se comparten a lo largo de toda la imagen, "
     "lo que reduce muchísimo la cantidad de parámetros y aporta invariancia a la traslación. Una red "
     "totalmente conectada trata cada píxel como una entrada independiente y no aprovecha esa estructura.", 3),
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


def _usuario(db, login, nombre, rol, email=""):
    """Devuelve (id, contraseña). La contraseña es None si el usuario ya existía."""
    fila = db.execute("SELECT * FROM users WHERE login = ?", (login,)).fetchone()
    if fila:
        return fila["id"], None
    password = auth.generate_password()
    uid = db.execute(
        "INSERT INTO users (login, password_hash, initial_password, full_name, email, role, active, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (login, auth.hash_password(password), password, nombre, email, rol, utcnow()),
    ).lastrowid
    return uid, password


def crear_demo(db):
    if db.execute("SELECT 1 FROM courses WHERE name = ?", (MATERIAS_DEMO[0][1],)).fetchone():
        print("Los datos de demostración ya existen.")
        return

    nombres = dict(MATERIAS_DEMO)
    ediciones = {}
    print(f"\nDiplomatura en IA Generativa — edición {EDICION_DEMO}:")
    for clave, nombre in MATERIAS_DEMO:
        cid = db.execute(
            "INSERT INTO courses (name, active, created_at) VALUES (?, 1, ?)", (nombre, utcnow())
        ).lastrowid
        ediciones[clave] = db.execute(
            "INSERT INTO course_editions (course_id, etiqueta, active, programa, fecha_inicio, fecha_fin,"
            " created_at) VALUES (?, ?, 1, ?, ?, ?, ?)",
            (cid, EDICION_DEMO, PROGRAMA_INTRO if clave == "intro" else "",
             FECHAS_DEMO.get(clave, ("", ""))[0], FECHAS_DEMO.get(clave, ("", ""))[1], utcnow()),
        ).lastrowid
        print(f"  · {nombre}")

    print("\nDocentes:")
    for login, nombre, materias in DOCENTES_DEMO:
        if login is None:
            fila = db.execute("SELECT id, full_name, login FROM users WHERE login = ?",
                              (ADMIN_LOGIN,)).fetchone()
            uid, password, login, nombre = fila["id"], None, fila["login"], fila["full_name"]
        else:
            uid, password = _usuario(db, login, nombre, "docente")
        for clave in materias:
            db.execute(
                "INSERT OR IGNORE INTO course_teachers (edition_id, user_id) VALUES (?, ?)",
                (ediciones[clave], uid),
            )
        clave_txt = f" · contraseña: {password}" if password else "  (cuenta ya existente)"
        print(f"  {nombre} → usuario: {login}{clave_txt}")
        print(f"    dicta: {', '.join(nombres[c] for c in materias)}")

    print("\nInstancias de evaluación:")
    for clave, instancias in INSTANCIAS_DEMO.items():
        for inst in instancias:
            aid = db.execute(
                "INSERT INTO assignments (edition_id, name, active, tipo, consigna, rubrica,"
                " max_integrantes, created_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                (ediciones[clave], inst["nombre"], inst["tipo"], inst["consigna"], inst["rubrica"],
                 inst.get("max_integrantes", 1), utcnow()),
            ).lastrowid
            extra = ""
            if inst["tipo"] == "escrito":
                for orden, (enunciado, respuesta, puntaje) in enumerate(PREGUNTAS_DL, 1):
                    db.execute(
                        "INSERT INTO assignment_items (assignment_id, orden, enunciado, respuesta, puntaje)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (aid, orden, enunciado, respuesta, puntaje),
                    )
                extra = f", {len(PREGUNTAS_DL)} preguntas · {sum(p for _, _, p in PREGUNTAS_DL)} puntos"
            elif inst.get("max_integrantes", 1) > 1:
                extra = f", grupos de hasta {inst['max_integrantes']}"
            print(f"  · {nombres[clave]} → {inst['nombre']} ({inst['tipo']}{extra})")

    print("\nEstudiantes (inscriptos en las cuatro materias):")
    for dni, nombre, email in ESTUDIANTES_DEMO:
        uid, password = _usuario(db, dni, nombre, "student", email)
        for eid in ediciones.values():
            enroll(db, uid, eid)
        print(f"  {nombre} → DNI: {dni} · contraseña: {password}")


def main():
    init_db()
    with get_db() as db:
        crear_admin(db)
        if "--demo" in sys.argv:
            crear_demo(db)
        else:
            print("(Para cargar la diplomatura de ejemplo con sus docentes y estudiantes: python seed.py --demo)")


if __name__ == "__main__":
    main()
