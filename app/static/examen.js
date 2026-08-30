// El examen que se rinde en la plataforma.
//
// Tres responsabilidades, y ninguna más: guardar lo escrito en el servidor mientras se
// trabaja, mostrar cuánto tiempo queda, y dejar constancia de dos cosas que pasan en la
// pantalla. No intenta bloquear nada: una página web no puede impedir que alguien cambie
// de aplicación o copie de otro lado, y fingir lo contrario sería peor que no hacer nada,
// porque daría una seguridad que no existe.
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
  // Se registran, se avisan, y no bloquean. El aviso es inmediato a propósito: enterarse
  // al final de que algo quedó anotado sería una trampa; enterarse en el momento permite
  // corregir la conducta o pedir explicaciones.
  function avisar(texto) {
    elAviso.textContent = texto;
    elAviso.hidden = false;
  }

  function registrar(tipo, detalle) {
    const cuerpo = JSON.stringify({ tipo, detalle });
    // `sendBeacon` sobrevive a que la pestaña se cierre o se oculte, que es justo cuando
    // más falta hace registrar: un fetch normal se cancela con la página.
    if (navigator.sendBeacon) {
      navigator.sendBeacon(`${base}/examen/${aid}/incidente`,
        new Blob([cuerpo], { type: "application/json" }));
      // El aviso se arma acá porque el beacon no devuelve respuesta.
      avisar(tipo === "salida"
        ? "Quedó registrado que saliste de la pantalla del examen. El equipo docente lo va a ver junto a tu entrega."
        : "Quedó registrado que pegaste texto desde otro lado. El equipo docente lo va a ver junto a tu entrega.");
      return;
    }
    fetch(`${base}/examen/${aid}/incidente`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: cuerpo,
    }).then(r => r.json()).then(d => { if (d && d.aviso) avisar(d.aviso); }).catch(() => {});
  }

  // Salir de la pantalla. Se pide que dure algo —dos segundos— para no anotar el
  // parpadeo de una notificación del sistema o un clic que devuelve el foco enseguida.
  let salioEn = null;
  const MINIMO_SALIDA = 2000;

  function seFue() { if (salioEn === null) salioEn = Date.now(); }
  function volvio() {
    if (salioEn === null) return;
    const fuera = Date.now() - salioEn;
    salioEn = null;
    if (fuera >= MINIMO_SALIDA) registrar("salida", { segundos: Math.round(fuera / 1000) });
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { seFue(); guardar(); } else { volvio(); }
  });
  window.addEventListener("blur", seFue);
  window.addEventListener("focus", volvio);

  // Pegar desde afuera. Se registra cuánto, no qué: el contenido es la respuesta de la
  // persona y no hay ninguna razón para guardarlo dos veces.
  campos.forEach(c => {
    c.addEventListener("paste", ev => {
      const texto = (ev.clipboardData || window.clipboardData)?.getData("text") || "";
      if (texto.length) registrar("pegado", { caracteres: texto.length });
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
          guardar().finally(() => {
            form.dataset.confirmado = "1";   // se acabó el tiempo: no hay nada que confirmar
            form.submit();
          });
        }
        return;
      }
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
