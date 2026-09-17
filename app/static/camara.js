// El visor para fotografiar las hojas de un examen, adentro de LidIA.
//
// Existe por una razón concreta, no por prolijidad: la causa número uno de una
// transcripción mala es la foto —torcida, cortada, borrosa, a contraluz—, y eso se
// descubre recién después de gastar la llamada al modelo y, a veces, el intento. Acá la
// hoja se encuadra contra un marco guía y cada disparo se mide antes de aceptarlo: si
// salió borrosa o muy oscura, se avisa en el momento, cuando volver a sacarla no cuesta
// nada.
//
// Lo que NO hace: bloquear. Una foto que el sistema considera fea puede ser perfectamente
// legible —una lapicera clara, una hoja gris—, y quien tiene la hoja delante sabe más que
// esta heurística. Se avisa y se deja seguir.
//
// Dónde no está: donde el navegador no da cámara —Safari sin HTTPS, navegadores viejos, una
// computadora sin webcam— el botón no aparece y quedan los dos caminos de siempre, la cámara
// del teléfono y la galería. Nadie se queda sin entregar por el navegador que le tocó.
(function () {
  const hay = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.isSecureContext);

  // Umbrales de la revisión. No salieron de la intuición: se midieron sobre la misma hoja
  // manuscrita degradada a propósito —desenfocada, a media luz, quemada, sin contraste— y se
  // eligieron para separar esos casos de la foto buena. Los valores de referencia, sobre una
  // copia de 400 px de ancho:
  //
  //     foto          nitidez   papel(p95)   rango   %quemado
  //     nítida           3346          251      59        1.7
  //     movida             39          250      54        0
  //     muy movida          4          250      41        0
  //     a media luz       849          126      30        0
  //     oscura            306           75      18        0
  //     quemada          1835          255      34       92
  //     sin contraste     213          245      15        0
  //
  // Ojo con la tentación de mirar la luminancia media: una hoja de papel bien iluminada da
  // 242 esté como esté, y por eso no distingue nada. Lo que habla es el nivel del papel, la
  // distancia entre el papel y la tinta, y cuánto se quemó.
  const PAPEL_MINIMO = 150;      // p95: por debajo, hasta el papel salió gris
  const RANGO_MINIMO = 28;       // p95 - p5: la tinta tiene que despegarse del papel
  const QUEMADO_MAXIMO = 25;     // % de píxeles en 255: un reflejo tapa el trazo
  const NITIDEZ_MINIMA = 60;     // varianza del laplaciano: por debajo, está movida

  function medir(canvas) {
    // Se mide sobre una copia chica: alcanza para saber si hay foco, luz y contraste, y evita
    // recorrer millones de píxeles en un teléfono.
    const ancho = 400, alto = Math.max(2, Math.round(canvas.height * (ancho / canvas.width)));
    const c = document.createElement("canvas");
    c.width = ancho; c.height = alto;
    const ctx = c.getContext("2d");
    ctx.drawImage(canvas, 0, 0, ancho, alto);
    const d = ctx.getImageData(0, 0, ancho, alto).data;

    const gris = new Float32Array(ancho * alto);
    const histograma = new Uint32Array(256);
    for (let i = 0, p = 0; i < d.length; i += 4, p++) {
      const g = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
      gris[p] = g;
      histograma[Math.min(255, Math.max(0, Math.round(g)))]++;
    }
    const total = gris.length;
    const percentil = frac => {
      let acumulado = 0, tope = frac * total;
      for (let v = 0; v < 256; v++) {
        acumulado += histograma[v];
        if (acumulado >= tope) return v;
      }
      return 255;
    };
    const p5 = percentil(0.05), p95 = percentil(0.95);
    const quemado = (histograma[254] + histograma[255]) / total * 100;

    // Varianza del laplaciano: cuánto cambia el brillo entre píxeles vecinos. Una foto
    // enfocada tiene bordes marcados y esa varianza es alta; una movida los tiene lavados.
    let suma = 0, suma2 = 0, n = 0;
    for (let y = 1; y < alto - 1; y++) {
      for (let x = 1; x < ancho - 1; x++) {
        const i = y * ancho + x;
        const l = 4 * gris[i] - gris[i - 1] - gris[i + 1] - gris[i - ancho] - gris[i + ancho];
        suma += l; suma2 += l * l; n++;
      }
    }
    const media = n ? suma / n : 0;
    return { nitidez: n ? suma2 / n - media * media : 0, papel: p95, rango: p95 - p5, quemado };
  }

  // El orden importa: una foto oscura también da poco contraste, y decirle «falta contraste»
  // a alguien que está en penumbra no lo ayuda a arreglarla. Primero la causa, después el efecto.
  function queja({ nitidez, papel, rango, quemado }) {
    if (papel < PAPEL_MINIMO) return "Hay poca luz: hasta el papel salió gris. Buscá una ventana o prendé una lámpara.";
    if (quemado > QUEMADO_MAXIMO) return "Un reflejo está tapando el texto. Movete un poco o corré la hoja de la luz directa.";
    if (rango < RANGO_MINIMO) return "El texto casi no se despega del papel. Acercate un poco y evitá la sombra de tu mano.";
    if (nitidez < NITIDEZ_MINIMA) return "Salió movida. Apoyá los codos, esperá a que enfoque y sacala de nuevo.";
    return "";
  }

  // Una foto del stream. Con ImageCapture se pide la foto de verdad —resolución del sensor,
  // no del video—, que en un teléfono es bastante mejor para leer letra manuscrita. Donde no
  // está (Safari, hoy), se toma el cuadro del video, que alcanza.
  async function disparar(video, track) {
    if (window.ImageCapture && track) {
      try {
        const bitmap = await new ImageCapture(track).grabFrame();
        const c = document.createElement("canvas");
        c.width = bitmap.width; c.height = bitmap.height;
        c.getContext("2d").drawImage(bitmap, 0, 0);
        bitmap.close?.();
        return c;
      } catch (e) { /* algunos navegadores lo anuncian y lo rechazan: sigue abajo */ }
    }
    const c = document.createElement("canvas");
    c.width = video.videoWidth; c.height = video.videoHeight;
    c.getContext("2d").drawImage(video, 0, 0);
    return c;
  }

  const comoArchivo = (canvas, n) => new Promise(resolve =>
    canvas.toBlob(b => resolve(new File([b], `hoja-${String(n).padStart(2, "0")}.jpg`,
      { type: "image/jpeg", lastModified: Date.now() })), "image/jpeg", 0.92));

  function armarPantalla() {
    const d = document.createElement("dialog");
    d.className = "camara";
    d.innerHTML = `
      <div class="camara-visor">
        <video playsinline muted autoplay></video>
        <div class="camara-marco"><span>Poné la hoja entera adentro del marco</span></div>
        <p class="camara-queja" hidden></p>
      </div>
      <div class="camara-barra">
        <button type="button" class="btn ghost" data-cerrar>Cancelar</button>
        <button type="button" class="camara-disparo" data-disparar aria-label="Sacar la foto"></button>
        <button type="button" class="btn accent" data-listo>Listo (<span data-cuenta>0</span>)</button>
      </div>
      <ol class="camara-tira" data-tira></ol>`;
    document.body.appendChild(d);
    return d;
  }

  // Abre el visor y resuelve con las hojas que se hayan sacado. `yaHay` es cuántas tenía
  // antes, para numerarlas seguido y para respetar el tope.
  async function abrir(yaHay, tope) {
    const d = armarPantalla();
    const video = d.querySelector("video"), tira = d.querySelector("[data-tira]"),
          cuenta = d.querySelector("[data-cuenta]"), quejaEl = d.querySelector(".camara-queja");
    const sacadas = [];
    let stream = null, track = null;

    function pintar() {
      cuenta.textContent = sacadas.length;
      tira.innerHTML = "";
      sacadas.forEach((s, i) => {
        const li = document.createElement("li");
        const img = document.createElement("img");
        img.src = s.url; img.alt = `Hoja ${yaHay + i + 1}`;
        const b = document.createElement("button");
        b.type = "button"; b.className = "camara-quitar"; b.textContent = "✕";
        b.setAttribute("aria-label", `Descartar la hoja ${yaHay + i + 1}`);
        b.addEventListener("click", () => { URL.revokeObjectURL(s.url); sacadas.splice(i, 1); pintar(); });
        li.append(img, b);
        const n = document.createElement("span"); n.textContent = `Hoja ${yaHay + i + 1}`;
        li.appendChild(n);
        tira.appendChild(li);
      });
      d.querySelector("[data-disparar]").disabled = yaHay + sacadas.length >= tope;
    }

    function cerrar() {
      stream?.getTracks().forEach(t => t.stop());
      d.close(); d.remove();
    }

    try {
      stream = await navigator.mediaDevices.getUserMedia({
        // La cámara de atrás, y la resolución más alta que el aparato ofrezca: el modelo lee
        // trazo fino, y lo que se pierde acá no se recupera después.
        video: { facingMode: { ideal: "environment" },
                 width: { ideal: 3840 }, height: { ideal: 2160 } },
        audio: false,
      });
    } catch (e) {
      d.remove();
      throw new Error(e && e.name === "NotAllowedError"
        ? "No nos diste permiso para usar la cámara. Podés sacar la foto con la cámara del teléfono y subirla."
        : "No pudimos abrir la cámara. Probá con la cámara del teléfono y subí la foto.");
    }
    video.srcObject = stream;
    track = stream.getVideoTracks()[0];
    d.showModal();
    pintar();

    return new Promise(resolve => {
      d.querySelector("[data-disparar]").addEventListener("click", async () => {
        const canvas = await disparar(video, track);
        const problema = queja(medir(canvas));
        const archivo = await comoArchivo(canvas, yaHay + sacadas.length + 1);
        sacadas.push({ archivo, url: URL.createObjectURL(archivo) });
        pintar();
        quejaEl.textContent = problema
          ? problema + " Si te parece que se lee igual, seguí; si no, descartala con la ✕."
          : "";
        quejaEl.hidden = !problema;
      });
      d.querySelector("[data-listo]").addEventListener("click", () => {
        cerrar(); resolve(sacadas.map(s => s.archivo));
      });
      d.querySelector("[data-cerrar]").addEventListener("click", () => {
        sacadas.forEach(s => URL.revokeObjectURL(s.url));
        cerrar(); resolve([]);
      });
      // Esc cierra el <dialog> solo: que eso signifique cancelar, no perder las fotos en silencio.
      d.addEventListener("cancel", ev => { ev.preventDefault(); });
    });
  }

  window.LidiaCamara = { disponible: hay, abrir };
})();
