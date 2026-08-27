# LidIA 🧠

**Devoluciones de entregas con IA** — Laboratorio de Investigación en Ciencia de Datos & Inteligencia Artificial (LICDIA) · UNLu

Lidia es la asistente del equipo docente: cada estudiante le entrega una versión completa de su trabajo y recibe una devolución formativa. La corrección final siempre la revisa y la firma un docente humano.

## El flujo

1. **Devoluciones de práctica** — cada estudiante tiene un cupo por instancia de evaluación (default **3**). Cada una evalúa una *entrega completa*: no es un chat de avances; presentar algo inmaduro también consume el intento (y esa es parte de la lección).
2. **Preguntas aclaratorias** — sobre cada devolución, hasta **3** preguntas (default). Lidia aclara su devolución pero no evalúa contenido nuevo ni reescribe el trabajo: eso es para la próxima entrega.
3. **Entrega final** — no consume prácticas. La IA propone una devolución que **solo ven los docentes**; un docente la edita/aprueba y recién ahí le llega al estudiante (en la app y por correo si hay SMTP configurado). También puede *reabrir* la entrega para pedir una nueva versión.

## Cursos, instancias de evaluación y tipos

* **Cursos** — el contenedor: docentes asignados y estudiantes inscriptos. Un curso puede tener varios docentes y un estudiante puede estar en varios cursos.
* **Instancias de evaluación** — cada curso tiene las que necesite (TP1, parcial, trabajo final…), cada una con su consigna, su material de corrección y sus cupos. Nacen como *borrador* (invisibles) y se activan cuando están listas. **Las configuran los docentes del curso.**
* **Tipos de instancia**:
  * *Trabajo abierto* — se evalúa contra consigna + rúbrica (sin nota numérica; devolución formativa por criterio).
  * *Examen escrito* — preguntas como ítems, cada una con su respuesta esperada y su **puntaje**. En las prácticas Lidia señala el error y orienta el repaso **sin revelar la respuesta**; en la corrección final revela cada respuesta correcta y propone una calificación por puntaje.
  * *Multiple choice* — preguntas con opciones y clave por ítem, y **una única oportunidad de entrega**, corregida contra la clave con calificación sugerida y firma docente.
* **Exámenes en papel** 📷 — en escrito y multiple choice, el estudiante puede subir **fotos de su examen resuelto a mano** (hasta 6, JPG/PNG). Un modelo con visión lo transcribe fielmente (sin corregir nada) y el estudiante **revisa la lectura antes de confirmar**: nada se registra ni consume cupo hasta ese momento. La transcripción no se edita — lo que vale es el papel, que el docente conserva para cotejar.
* **Cuenta propia** — cualquier usuario cambia su contraseña desde su nombre en la barra superior. La contraseña inicial generada se muestra en la ficha (docente o estudiante) hasta que la persona la cambia.
* **Bajas con historial protegido** — cursos, docentes y estudiantes se pueden eliminar solo si no tienen entregas (o firmas de corrección) asociadas; si las tienen, el camino es deshabilitar o desactivar. Las instancias, igual.
* Las respuestas esperadas / clave son material interno: el estudiante nunca las ve, y las preguntas aclaratorias tampoco pueden filtrarlas (el estándar no viaja en ese contexto).

## Roles y usuarios

* **Roles** — `admin` (coordinación: ve todo y administra cursos, docentes y configuración), `docente` (administra solo sus cursos: instancias, entregas, estudiantes) y `student`.
* **Estudiantes** — alta individual o masiva (archivo CSV o pegando el listado `DNI, Apellido y Nombre, correo`, directo desde una planilla), siempre dentro de un curso. Si el DNI ya existe, solo se lo inscribe. Habilitar/deshabilitar es un clic: quien está deshabilitado conserva su historial y ve un aviso amable para regularizar lo administrativo (texto editable).

## Correr en 2 minutos (modo demo, sin API key)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
LIDIA_ADMIN_LOGIN=tu-usuario LIDIA_ADMIN_NAME="Apellido, Nombre" python seed.py --demo
uvicorn app.main:app --port 8080 --env-file .env
```

El `seed.py` crea el usuario de coordinación y **genera su contraseña al azar, mostrándola una sola
vez**; con `--demo` agrega además un curso de ejemplo con tres estudiantes de prueba (omitilo en una
instalación real). Abrí http://localhost:8080 y entrá con esas credenciales.

Sin `LLM_API_KEY` la app corre en **modo demo**: todo el flujo funciona con devoluciones de ejemplo,
ideal para mostrar la herramienta sin costo.

## Conectar un modelo real

Copiá `.env.example` a `.env` y completá (API compatible con OpenAI, proveedor a elección):

```
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4.1-mini
```

En `.env.example` hay ejemplos para Gemini (capa gratuita), Claude, DeepSeek y **Ollama local** (si la universidad prefiere que ningún dato salga a terceros). Ojo: `uvicorn` solo lee el `.env` con `--env-file .env` (en Docker lo inyecta compose).

**Costo estimado** con `gpt-4.1-mini`: una devolución completa ronda los 15–20 k tokens de entrada y ~1,5 k de salida → **~1 centavo de dólar**. Dos cohortes completas (55 estudiantes × 3 prácticas + preguntas + final) difícilmente superen los **US$ 3–5** en total.

## Docker

```bash
cp .env.example .env   # completar
docker compose up -d --build
docker compose exec lidia python seed.py
```

## Despliegue bajo un prefijo (`tu-dominio/entregas`)

La app se sirve bajo un prefijo con la variable `BASE_PATH`, que prefija enlaces, redirecciones y la cookie de
sesión. En desarrollo se deja vacía y todo queda en la raíz.

```
BASE_PATH=/entregas
```

El proxy debe pasar el path **completo** (sin recortar el prefijo):

```nginx
location /entregas/ {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    client_max_body_size 20m;   # las entregas y los exámenes se suben como archivo
    proxy_read_timeout 300s;    # una devolución tarda entre 30 y 90 segundos
}
```

## Administración

* **Cursos** — una **materia** tiene varias **cursadas** (2025, 2026, contracursada…). La coordinación las crea y asigna docentes. Cada cursada tiene su **programa** (contenidos, bibliografía, equipo docente): Lidia lo usa como contexto en todas sus instancias, para situar los comentarios en las unidades que se dieron y no reclamar temas que el curso no cubrió. El programa no es criterio de evaluación: el estándar es la consigna y la rúbrica.
* **Instancias de evaluación** — cada cursada tiene las suyas (TP, parcial, propuesta, trabajo final), con su tipo, consigna, rúbrica o respuestas esperadas, cupos y cantidad máxima de integrantes. Las gestionan los docentes de la cursada desde su ficha.
* **Docentes** — ABM de la coordinación: usuario, contraseña generada, cursos a cargo.
* **Estudiantes** — alta individual, CSV o pegado masivo; ficha con correo, inscripciones y el **perfil de corrección**: orientación de la devolución para esa persona (más técnica, más pedagógica…). Ajusta tono y foco; el estándar de exigencia es el mismo para todo el grupo.
* **Configuración** — lo global: aviso para deshabilitados y si se envía el nombre de pila al modelo.

## Investigación

LidIA registra, mientras se usa, lo que después no se puede reconstruir: la configuración exacta con la que se generó cada devolución, el costo y la latencia del modelo, **cuánto reescribió el equipo docente la propuesta de la IA antes de firmarla**, si el estudiantado la leyó y si le sirvió.

El estudiantado decide en «Tu cuenta» si sus entregas pueden usarse; sin ese consentimiento explícito quedan fuera de toda exportación, y usa el sistema exactamente igual. Lo exportado va seudonimizado: cada persona aparece con un código estable por instalación que no permite volver a ella.

## Pendiente (ideas en agenda)

* **Foto del examen en papel**: que el estudiante suba una foto de su examen manuscrito y un modelo con visión lo transcriba para corregirlo contra el estándar — pensado para los cursos de grado (Sistemas Inteligentes, IA, ML), donde el parcial se hace con hoja y lapicera y LidIA los hace parte de una experiencia real de IA.

## Datos personales

* El **DNI nunca se envía al modelo**: es solo identificador de acceso local.
* Al modelo van: el texto del trabajo, el nombre de pila (desactivable en Configuración para anonimizar por completo) y el perfil de corrección.
* Con un proveedor externo, el texto del trabajo sale de la universidad; si eso es un problema, la app funciona igual apuntando `LLM_BASE_URL` a un modelo alojado por la propia institución (Ollama u otro endpoint compatible).

## Antes de producción

Este es un prototipo pensado para demo interna. Para producción: cambiar la contraseña de coordinación y `SESSION`-cookies detrás de HTTPS; quitar la columna `initial_password` (existe solo para repartir credenciales la primera vez); agregar CSRF tokens y rate-limiting; respaldar `data/lidia.db`; y mover la generación de devoluciones a una cola si el curso crece (hoy el pedido se procesa durante el request, ~30–90 s).

---

*La devolución de Lidia es orientativa: la calificación final la define un docente humano 🧑‍🏫*
