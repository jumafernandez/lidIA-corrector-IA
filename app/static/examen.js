// El examen que se rinde en la plataforma.
//
// Tres responsabilidades: guardar lo escrito en el servidor mientras se trabaja, mostrar
// cuánto tiempo queda, y dejar constancia de dos cosas que pasan en la pantalla.
//
// Se bloquea pegar texto y se pide pantalla completa. Del resto no se puede: una página web
// no puede impedir que alguien cambie de aplicación, lea de otra pantalla o tenga el
// teléfono al lado, y fingir lo contrario sería peor que no hacer nada, porque daría una
// seguridad que no existe. Lo que sí se hace es que no pueda decir que no se enteró.
(function () {
  const cab = document.querySelector("[data-examen]");
  if (!cab) return;
  const aid = cab.dataset.examen;
  const base = cab.dataset.base || "";
  const form = document.getElementById("form-examen");
  const elGuardado = document.getElementById("guardado");
  const elReloj = document.getElementById("reloj");
  const elAviso = document.getElementById("aviso-incidente");
  const elConexion = document.getElementById("aviso-conexion");
  const campos = [...form.querySelectorAll("[data-orden]")];

  // ---------------------------------------------------------------- guardado
  //
  // Lo escrito vive en el servidor: si se corta la luz o se cierra la pestaña, quien
  // vuelve —acá o en otra máquina— encuentra lo que había. Por eso se guarda seguido y
  // no solo al entregar.
  let sucio = false, guardando = false, fallos = 0;

  function respuestas() {
    const r = {};
    campos.forEach(c => {
      if (c.type === "radio") { if (c.checked) r[c.dataset.orden] = c.value; }
      else if (c.value.trim()) r[c.dataset.orden] = c.value;
    });
    return r;
  }

  function hora() {
    return new Date().toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
  }

  async function guardar() {
    if (!sucio || guardando) return;
    guardando = true;
    const enviado = respuestas();
    try {
      const r = await fetch(`${base}/examen/${aid}/guardar`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ respuestas: enviado }),
      });
      if (!r.ok) throw new Error(String(r.status));
      sucio = false; fallos = 0;
      elConexion.hidden = true;
      elGuardado.textContent = "Guardado " + hora();
      elGuardado.classList.remove("sin-guardar");
    } catch (e) {
      // Se reintenta solo: el próximo ciclo lo vuelve a tomar porque `sucio` sigue en pie.
      fallos++;
      if (fallos >= 2) elConexion.hidden = false;
      elGuardado.textContent = "Sin guardar";
      elGuardado.classList.add("sin-guardar");
    } finally {
      guardando = false;
    }
  }

  campos.forEach(c => {
    c.addEventListener("input", () => { sucio = true; crecer(c); });
    c.addEventListener("change", () => { sucio = true; });
  });
  setInterval(guardar, 5000);
  window.addEventListener("blur", guardar);

  // Los cuadros de respuesta crecen con lo que se escribe: una barra de scroll adentro de
  // un campo obliga a releer a ciegas lo que uno acaba de escribir.
  function crecer(c) {
    if (c.tagName !== "TEXTAREA") return;
    c.style.height = "auto";
    c.style.height = Math.max(c.scrollHeight + 2, 120) + "px";
  }
  campos.forEach(crecer);

  // ---------------------------------------------------------------- incidentes
  //
  // El aviso es inmediato a propósito: enterarse al final de que algo quedó anotado sería
  // una trampa; enterarse en el momento permite corregir la conducta o pedir explicaciones.
  // Y por eso la salida tapa el examen hasta que se la cierre, en vez de una banda que se
  // puede pasar por alto si uno está escribiendo más abajo.
  const elBloqueo = document.getElementById("bloqueo");
  const elBloqueoTitulo = document.getElementById("bloqueo-titulo");
  const elBloqueoTexto = document.getElementById("bloqueo-texto");
  const elBloqueoCerrar = document.getElementById("bloqueo-cerrar");
  const elContador = document.getElementById("contador");

  // Salir de la pantalla. Se pide que dure algo —dos segundos— para no anotar el
  // parpadeo de una notificación del sistema o un clic que devuelve el foco enseguida.
  let salioEn = null;
  const MINIMO_SALIDA = 2000;
  let cerrando = false;          // entregando o yéndose: lo que pase después ya no cuenta
  let pantallaPerdida = false;   // se salió de pantalla completa mientras estaba afuera

  function avisar(texto) {
    elAviso.textContent = texto;
    elAviso.hidden = false;
  }

  // ---------------------------------------------------------------- pantalla completa
  //
  // El examen se rinde en pantalla completa: ahí no hay barra de pestañas ni botones de
  // minimizar a la vista, y salir de ella es algo que el navegador avisa. No impide nada
  // —un segundo monitor o un teléfono siguen ahí—, pero el «miré otra ventana de reojo»
  // pasa a ser un acto deliberado que queda anotado.
  //
  // Solo donde el navegador la ofrece. En el iPhone no existe para páginas, y dejar a
  // alguien sin rendir por su teléfono sería castigar a quien menos tiene.
  const elPuerta = document.getElementById("puerta");
  const conPantalla = !!(document.fullscreenEnabled && document.documentElement.requestFullscreen);

  function enPantalla() { return !!document.fullscreenElement; }
  function pedirPantalla() {
    if (!conPantalla || enPantalla()) return Promise.resolve();
    return document.documentElement.requestFullscreen({ navigationUI: "hide" }).catch(() => {});
  }

  if (!conPantalla) {
    elPuerta.hidden = true;
  } else {
    document.getElementById("puerta-entrar").addEventListener("click", () => {
      pedirPantalla().then(() => { elPuerta.hidden = true; });
    });
    document.addEventListener("fullscreenchange", () => {
      if (enPantalla() || cerrando || !elPuerta.hidden) return;
      // Si además está fuera de la pestaña, la salida se anota por el otro camino cuando
      // vuelva; acá solo se recuerda que perdió la pantalla completa.
      if (salioEn !== null) { pantallaPerdida = true; return; }
      registrar("pantalla", {});
    });
  }

  // Volver de afuera tapa el examen hasta que se cierre con un clic. Sin temporizador: si
  // la salida fue un falso positivo, no le cuesta tiempo a quien no hizo nada. El botón
  // devuelve a pantalla completa cuando se la perdió: es un clic, y eso el navegador lo acepta.
  function bloquear(tipo, detalle) {
    elBloqueoTitulo.textContent = tipo === "pantalla"
      ? "Saliste de la pantalla completa" : "Saliste de la pantalla del examen";
    elBloqueoTexto.textContent = detalle || "";
    elBloqueoTexto.hidden = !detalle;
    elBloqueoCerrar.textContent = conPantalla && !enPantalla()
      ? "Volver al examen en pantalla completa" : "Volver al examen";
    elBloqueo.hidden = false;
    elBloqueoCerrar.focus();
  }
  elBloqueoCerrar.addEventListener("click", () => {
    pedirPantalla().then(() => { elBloqueo.hidden = true; pantallaPerdida = false; });
  });

  function pintarContador(llevados) {
    if (!llevados) return;
    const s = llevados.salida || 0, p = llevados.pegado || 0, f = llevados.pantalla || 0;
    if (!s && !p && !f) { elContador.hidden = true; return; }
    const partes = [];
    if (s) partes.push(`${s} salida${s === 1 ? "" : "s"} de la pantalla`);
    if (f) partes.push(`${f} salida${f === 1 ? "" : "s"} de pantalla completa`);
    if (p) partes.push(`${p} intento${p === 1 ? "" : "s"} de pegar`);
    elContador.textContent = "Registrado en este examen: " + partes.join(" · ");
    elContador.hidden = false;
  }
  pintarContador({ salida: +elContador.dataset.salidas, pegado: +elContador.dataset.pegados,
                   pantalla: +elContador.dataset.pantallas });

  // Se registra con fetch y no con sendBeacon porque hace falta la respuesta: el servidor
  // devuelve cuántos van, y ese número es el que se muestra. La salida se envía al VOLVER,
  // con la pestaña ya visible, así que no hay riesgo de que el envío se cancele.
  function registrar(tipo, detalle) {
    return fetch(`${base}/examen/${aid}/incidente`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tipo, detalle }),
    }).then(r => r.json()).then(d => {
      if (!d || !d.ok) return;
      pintarContador(d.llevados);
      if (tipo === "pegado") avisar(d.aviso); else bloquear(tipo, d.detalle);
    }).catch(() => {
      // Sin conexión no se pierde el aviso, aunque el conteo quede para el próximo.
      if (tipo === "pegado") avisar("No se puede pegar texto en el examen. Quedó registrado el intento.");
      else bloquear(tipo, "");
    });
  }

  function seFue() { if (salioEn === null) salioEn = Date.now(); }
  function volvio() {
    if (salioEn === null) return;
    const fuera = Date.now() - salioEn;
    salioEn = null;
    if (fuera >= MINIMO_SALIDA) registrar("salida", { segundos: Math.round(fuera / 1000) });
    else if (pantallaPerdida) registrar("pantalla", {});   // fue corto, pero la pantalla completa se perdió
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { seFue(); guardar(); } else { volvio(); }
  });
  window.addEventListener("blur", seFue);
  window.addEventListener("focus", volvio);
  form.addEventListener("submit", () => { cerrando = true; });
  window.addEventListener("pagehide", () => { cerrando = true; });

  // Pegar desde afuera: se impide y se anota el intento. Bloquear en el navegador no es
  // infranqueable —quien abra las herramientas de desarrollo lo saltea— pero convierte un
  // Cmd+V distraído en algo deliberado, que es exactamente lo que se busca. Se registra
  // cuánto, no qué: el texto es de la persona y no hay razón para guardarlo.
  //
  // El arrastrar-y-soltar va junto: es la otra forma de meter texto de afuera y no dispara
  // el mismo evento, así que bloquear solo el pegado dejaría la puerta de al lado abierta.
  campos.forEach(c => {
    c.addEventListener("paste", ev => {
      ev.preventDefault();
      const texto = (ev.clipboardData || window.clipboardData)?.getData("text") || "";
      registrar("pegado", { caracteres: texto.length, bloqueado: 1 });
    });
    c.addEventListener("drop", ev => {
      ev.preventDefault();
      const texto = ev.dataTransfer?.getData("text") || "";
      registrar("pegado", { caracteres: texto.length, bloqueado: 1 });
    });
  });

  // ---------------------------------------------------------------- reloj
  //
  // La cuenta va contra la hora del SERVIDOR: se calcula cuánto falta en el momento de
  // servir la página y desde ahí se descuenta. El reloj de la máquina de quien rinde
  // puede estar corrido, y con él se decidiría mal cuándo se acabó el examen.
  const cierre = cab.dataset.cierre;
  if (cierre) {
    const desfasaje = new Date(cab.dataset.ahora).getTime() - Date.now();
    const fin = new Date(cierre).getTime();
    let entregando = false;

    function tic() {
      const falta = fin - (Date.now() + desfasaje);
      if (falta <= 0) {
        elReloj.textContent = "0:00";
        if (!entregando) {
          entregando = true;
          // Se guarda antes de entregar para no perder lo último que se escribió, y
          // recién ahí se envía: lo que vale es lo que está en el servidor.
          sucio = true;
          cerrando = true;                   // salir de pantalla completa al entregar no es un incidente
          guardar().finally(() => {
            form.dataset.confirmado = "1";   // se acabó el tiempo: no hay nada que confirmar
            form.submit();
          });
        }
        return;
      }
      // Con más de un día por delante esto no es un examen con reloj, es una ventana: una
      // cuenta regresiva de tres cifras de horas no le dice nada a nadie. Va la fecha.
      if (falta > 24 * 3600 * 1000) {
        const d = new Date(fin);
        const dd = String(d.getDate()).padStart(2, "0"), mm = String(d.getMonth() + 1).padStart(2, "0");
        const hh = String(d.getHours()).padStart(2, "0"), mi = String(d.getMinutes()).padStart(2, "0");
        elReloj.textContent = `cierra el ${dd}/${mm} a las ${hh}:${mi}`;
        elReloj.classList.add("lejos");
        setTimeout(tic, 60000);
        return;
      }
      elReloj.classList.remove("lejos");
      const total = Math.floor(falta / 1000);
      const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), sg = total % 60;
      elReloj.textContent = h > 0
        ? `${h}:${String(m).padStart(2, "0")}:${String(sg).padStart(2, "0")}`
        : `${m}:${String(sg).padStart(2, "0")}`;
      elReloj.classList.toggle("apurado", total <= 300);
      setTimeout(tic, 1000);
    }
    tic();
  }

  // Guardar antes de irse, por si acaso. No se pregunta «¿seguro?»: el examen está en el
  // servidor, así que irse no pierde nada, y un cartel del navegador solo asustaría.
  window.addEventListener("pagehide", () => {
    if (!sucio) return;
    navigator.sendBeacon?.(`${base}/examen/${aid}/guardar`,
      new Blob([JSON.stringify({ respuestas: respuestas() })], { type: "application/json" }));
  });
})();
