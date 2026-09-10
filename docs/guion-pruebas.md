# Guion de pruebas de LidIA — para Francisco y Seba

Sistema: **https://licdia.unlu.edu.ar/entregas**. La idea es probarlo **de punta a punta**: ustedes crean la materia, la cursada, las instancias, los estudiantes; entregan, corrigen, validan. Nada viene armado.

## Reglas de juego

- Cada uno tiene cuatro cuentas, una por rol. Prueben de a dos —uno coordina o corrige, el otro entrega— y **a mitad del guion cambien de rol**: lo que no se ve desde un lado se ve desde el otro.
- **La cuenta de coordinación ve todas las cursadas del sistema, incluidas las que no son de ustedes.** Entren únicamente a las que crearon ustedes. Las de Juan y la de *Demostración SIED* (`alan_turing` / `ada_lovelace`) no se abren ni se tocan.
- Al entrar por primera vez, **cambien la clave** en *Mi cuenta* (arriba a la derecha).
- Al menos la mitad del recorrido del estudiante háganlo **desde el celular**: ahí es donde de verdad van a rendir y ahí es donde más se rompe.
- Cada devolución le cuesta unos centavos al Laboratorio. Prueben todo lo que quieran; no armen bucles de cincuenta entregas.
- Nombren lo que creen con sus nombres (*Materia de Fran*, *Cursada de Seba*), así se sabe de quién es cada cosa y se borra fácil después.

| Rol | Francisco | Seba | Clave inicial |
|---|---|---|---|
| Coordinación | `fran.coordinador` | `seba.coordinador` | `Pruebas.2026` |
| Docente | `fran.docente` | `seba.docente` | `Pruebas.2026` |
| Estudiante | `fran.estudiante` | `seba.estudiante` | `Pruebas.2026` |
| Segundo estudiante (para grupos) | `fran.estudiante2` | `seba.estudiante2` | `Pruebas.2026` |

## Los dieciséis escenarios

Cada uno dice qué hacer y qué debería pasar. Si pasa otra cosa, es un hallazgo. Si pasa lo esperado pero les resultó confuso o lento, también.

**Arranque**

1. **Entrar y salir.** Entren con cada cuenta, cambien la clave, cierren sesión, vuelvan a entrar con la nueva. Prueben *Olvidé mi contraseña*. → En producción **no hay correo configurado**: el sistema tiene que decirlo con claridad, no quedarse callado ni fingir que envió algo.

**Coordinación**

2. **Armar la cursada.** Como coordinación: creen una materia, después una cursada de esa materia (año y etiqueta), asígnenle al docente de ustedes. Den de alta un estudiante a mano y después importen un listado: peguen cinco filas, con una repetida y una con el documento mal escrito. → Un resumen claro de qué entró y qué no, y por qué. *Guardar y enviar aviso* tiene que avisar que no pudo enviar el correo.

3. **Los años.** Creen también una cursada con año 2025. → En el panel tiene que aparecer solo lo del año en curso; la de 2025 queda detrás del filtro *anteriores*. Cambien el filtro y cierren sesión: al volver tiene que recordar el que dejaron.

**Docente**

4. **Crear instancias.** Como docente, en su cursada: una **práctica abierta** desde cero (consigna y rúbrica escritas por ustedes, con criterios de verdad), un **escrito** desde la plantilla Word (bájenla, complétenla, súbanla), una de **opción múltiple**, y un escrito **en papel**. Configuren intentos y repreguntas distintos en cada una. → Cada instancia queda activa y el estudiante la ve en su panel con lo que corresponde a su modalidad.

**El circuito completo, como estudiante y como docente**

5. **Primera entrega de la práctica abierta.** Como estudiante, suban un PDF de dos o tres páginas que responda a la consigna que escribieron. → Devolución en menos de dos minutos, con *Lo que entregaste*, la evaluación criterio por criterio con su nivel, fortalezas y prioridades. El contador de intentos baja.

6. **Repreguntar.** Háganle preguntas a la devolución. → Respuestas pertinentes a *ese* trabajo, no genéricas. El contador baja; al llegar a cero, el formulario desaparece.

7. **Segunda y tercera entrega.** Cambien algo del documento y vuelvan a entregar. → La nueva devolución tiene que reflejar el cambio, no repetir la anterior. Al agotar los intentos, solo queda *entrega definitiva*.

8. **Entrega definitiva.** → El estudiante ve *En revisión docente*. El docente la ve aparecer en su cola de entregas.

9. **Revisión docente.** Como docente, abran esa definitiva: editen el texto de la devolución, cambien la calificación, validen. → El estudiante la ve *Aprobada*, con la nota nueva y **el texto editado**, no el original. Ya no puede volver a entregar.

10. **Sin revisión.** Creen una instancia con *requiere revisión* apagado y entreguen. → Se aprueba sola, con nota, sin pasar por el docente. Anoten si les parece que eso debería avisarse en algún lado.

11. **En grupo.** Instancia con hasta dos integrantes. Entreguen con `estudiante` sumando a `estudiante2`. → Los dos ven la misma entrega y la misma devolución; el docente ve una sola entrega con los dos nombres.

**Las modalidades que no son un archivo**

12. **Examen en papel.** Escriban a mano dos respuestas y sáquenles fotos con el celular: una con buena luz, una torcida, una borrosa. Súbanlas. → Aparece *Esto es lo que leí* con la transcripción: compárenla con la hoja, palabra por palabra. Confirmen → devolución con puntaje por pregunta. Prueben también siete fotos (el límite es seis) y una foto que no sea un examen.

13. **Opción múltiple.** Respondan, con dos o tres mal a propósito. → Nota automática correcta y una explicación escrita de cada error. Salgan a mitad de camino y vuelvan: ¿qué pasó con lo que habían marcado?

14. **Rendir en la plataforma.** Marquen un escrito como *en plataforma* con una ventana de dos días y ríndanlo como estudiante: miren el reloj, escriban, salgan de la pestaña, intenten pegar texto, cierren el navegador y vuelvan, dejen que se acabe el tiempo. **Anoten todo lo que les parezca raro**: hay cosas ahí que sabemos que están mal y queremos ver si las encuentran solos.

**Lo que rompe**

15. **Archivos raros.** Entreguen: un `.docx`, un notebook `.ipynb`, un PDF escaneado (solo imágenes, sin texto), un archivo vacío, uno de 30 MB, uno con acentos y espacios en el nombre. → Cada caso con un mensaje claro de qué pasó. **Ningún error 500, ninguna pantalla en blanco.**

16. **Cierre de cursada y celular.** Como coordinación: dupliquen su cursada para el año que viene, exporten las notas, den de baja a un estudiante → ya no puede entregar pero sigue viendo su historial; la cursada duplicada aparece vacía de entregas y con las instancias. Y para terminar, repitan los escenarios 5, 8, 12 y 13 **desde el teléfono**, en Chrome y en Safari → nada cortado, botones alcanzables con el pulgar, la foto se saca con la cámara directamente.

Cuando terminen, sigan libres. Lo que encuentran sin guion suele ser lo más valioso.

## Cómo reportar

Un **issue por hallazgo** en https://github.com/jumafernandez/lidIA-corrector-IA/issues — no varios en uno. Con este molde:

```
Título: [pantalla] lo que pasó, en diez palabras

Usuario e instancia:
Qué hice (pasos, en orden):
Qué esperaba:
Qué pasó (texto exacto del error, y captura):
Dispositivo y navegador:
Gravedad: bloquea · molesta · detalle
```

No es solo para errores. Lo que no entendieron, lo que tuvieron que leer dos veces, lo que les pareció lento, lo que les gustó: todo eso es un hallazgo y va también.
