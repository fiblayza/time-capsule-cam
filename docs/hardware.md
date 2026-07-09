# Montaje de hardware — La Cabina (Brondi Vintage 20)

Guía paso a paso para montar la cabina con un **Brondi Vintage 20** + Raspberry Pi 4 + cámara.

Ojo: el Vintage 20 es un teléfono retro *moderno* — la ruleta es decorativa (marca por botones), y por dentro lleva una placa electrónica actual, no la regleta de tornillos de un teléfono antiguo. El cableado del [proyecto upstream](https://github.com/nickpourazima/rotary-phone-audio-guestbook/blob/main/docs/hardware.md) (terminales B/L2, mic de carbón) **no aplica aquí**: en su lugar se suelda al microinterruptor del gancho y se pincha el audio del auricular. Es más trabajo de soldador, pero a cambio el micrófono moderno suena mejor que el de carbón de los teléfonos de época.

El teléfono **nunca se conecta a la línea telefónica**: usamos solo la carcasa, el auricular y el interruptor del gancho. Su placa queda sin alimentar.

---

## 1. Materiales

| Pieza | Notas | Cant. |
|---|---|---|
| Brondi Vintage 20 | Cualquier color; misma electrónica. | 1 |
| Raspberry Pi 4 (1 GB basta) | El sistema completo grabando usa ~500-600 MB. El instalador amplía la swap a 1 GB como red de seguridad; con 2 GB tienes margen para no pensar. (El upstream usa una Pi Zero, pero audio + H.264 simultáneos la superan.) | 1 |
| Disipador o caja con ventilación | Obligatorio: ffmpeg mantiene un núcleo al 60–80 %. Objetivo < 70 °C. | 1 |
| microSD 32 GB | ~450 MB/h de vídeo a 720p — 32 GB sobran para un día entero. | 1 |
| Adaptador de audio USB | Tipo [Adafruit 1475](https://www.adafruit.com/product/1475) o similar, con entrada de mic y salida de auricular en 3,5 mm. | 1 |
| Cámara | En orden de preferencia: Módulo CSI RPi Camera 3 (H.264 por hardware, menos CPU) · webcam USB (p. ej. C920) · mini cámara USB "espía" (más fácil de esconder). | 1 |
| Conectores 3,5 mm macho a bornes de tornillo | Para llevar el audio del auricular al adaptador USB sin soldar los jacks. | 2 |
| Soldador + estaño | Imprescindible: el gancho del Vintage 20 obliga a soldar 2 hilos en su placa. | — |
| Cable fino (p. ej. dupont hembra) | 4-6 hilos para gancho y LED; con terminal dupont se pinchan directos a la cabecera GPIO. | — |
| LED + resistencia 220 Ω | Opcional: indicador de grabación. | 1 |
| Pendrive USB (exFAT/FAT32) | Opcional: copia de seguridad automática de cada sesión. | 1 |
| Fuente USB-C 5 V/3 A oficial | La Pi 4 con cámara USB es exigente; no escatimes aquí. | 1 |
| Multímetro | Para identificar los contactos del gancho y los hilos del auricular. | — |

---

## 2. Vista general de conexiones

```
                        Raspberry Pi 4 (cabecera GPIO, pines físicos)
                        ┌──────────────────────────────┐
 Brondi Vintage 20      │  pin 11 (GPIO17) ──[220Ω]──► LED grabación (opcional)
 ┌────────────────┐     │  pin 14 (GND)    ◄────────── contacto A del microswitch
 │ Microswitch    ├─────┤  pin 15 (GPIO22) ◄────────── contacto B del microswitch
 │ del gancho     │     │  pin 16 (GPIO23) ◄────────── botón regrabar saludo (opcional)
 │ (en la placa)  │     │  pin 9  (GND)    ◄────────── GND del LED y del botón
 └────────────────┘     └──────────────────────────────┘
 ┌────────────────┐
 │ Auricular      │── par del mic ─────► entrada mic (rosa)  ┐
 │ (cable rizado, │                                          ├─ Adaptador audio USB ─► USB Pi
 │  4 hilos)      │── par del altavoz ─► salida auric (verde)┘
 └────────────────┘
 Cámara (CSI o USB) ──────────────────────────────────────────► CSI / USB
 Pendrive de respaldo (opcional) ─────────────────────────────► USB
```

Los números de GPIO son BCM (los que usa `config.yaml`); los "pin" son la posición física en la cabecera de 40 pines.

---

## 3. Abrir el teléfono y localizar el gancho

1. Desenchufa cualquier cable de línea (RJ11) y no lo vuelvas a conectar nunca.
2. Quita los tornillos de la base (debajo, a veces alguno oculto bajo los tacos de goma) y levanta la carcasa.
3. Localiza el **microinterruptor del gancho**: está en la placa, justo debajo del émbolo de plástico que el auricular presiona al reposar. Cuelga y descuelga el auricular y verás el émbolo actuar sobre él.
4. Identifica los contactos con el multímetro en continuidad (el teléfono sin alimentar). Los microswitch suelen tener 3 patas (común + NC + NO); busca el **par que cambia** al presionar el émbolo:
   - Continuidad con el auricular **colgado** que se corta al descolgar → configura `hook_type: NC`
   - Continuidad solo al **descolgar** → `hook_type: NO`
5. Apunta el par de patas y el tipo. Si te sale al revés en las pruebas finales, no hay que resoldar: pon `invert_hook: true` en `config.yaml`.

---

## 4. Cablear el gancho a la Pi

1. Suelda un hilo a cada pata del par identificado (con el soldador rápido, sin recalentar el switch).
2. Saca los dos hilos por el hueco del cable de línea de la carcasa.
3. Conéctalos a la Pi — el orden de los dos da igual, es un contacto seco:

| Desde | Hasta |
|---|---|
| Pata A del microswitch | **GND** — pin físico 14 |
| Pata B del microswitch | **GPIO 22** — pin físico 15 |

No hace falta resistencia: el software activa la pull-up interna del pin. Ambos procesos (audio del upstream y vídeo de esta extensión) leen este mismo pin de forma independiente.

> El resto de la placa del teléfono (teclado, timbre, melodías) queda sin uso y sin alimentación. No desueldes nada más: el switch puede seguir soldado a su placa, los contactos son libres de tensión al no haber línea conectada.

---

## 5. Audio del auricular

El cable rizado del auricular lleva 4 hilos: un par para el micrófono y otro para el altavoz. Vamos a desviarlos al adaptador de audio USB en vez de a la placa del teléfono.

1. Localiza dónde muere el cable rizado: o bien en un conector en la placa de la base, o soldado a ella. Desconéctalo/desuéldalo de la placa — esos 4 hilos son ahora nuestros.
   - Alternativa sin tocar la base: abre el **auricular** (tornillos o rosca en las cápsulas) y saca cable nuevo desde las propias cápsulas.
2. Identifica los pares con el multímetro en continuidad, abriendo el auricular para ver qué hilo llega a cada cápsula:
   - Cápsula pequeña con dos hilos hacia la boquilla → **micrófono**
   - Cápsula grande del lado de la oreja → **altavoz**
3. Conecta al adaptador USB con los conectores 3,5 mm de bornes:
   - Par del mic → conector en la **entrada de mic (rosa)**: hilo de señal al borne T (punta), retorno al S (manguito)
   - Par del altavoz → conector en la **salida de auricular (verde)**: igual, T y S
4. El mic del Vintage 20 es de tipo electret y se alimenta del propio bias que da la entrada de mic del adaptador USB. **Si no capta nada, invierte los dos hilos del par del mic** (los electret tienen polaridad).
5. Enchufa el adaptador a la Pi y comprueba: `aplay -l` (apunta el número de tarjeta; si no es `plughw:1,0`, ajusta `alsa_hw_mapping` en `config.yaml`). Prueba de mic rápida: `arecord -D plughw:1,0 -d 3 test.wav && aplay -D plughw:1,0 test.wav` hablando por el auricular.

A diferencia de los teléfonos de ruleta antiguos, aquí **no hace falta sustituir el micrófono**: la cápsula moderna del Brondi da calidad más que suficiente.

---

## 6. Cámara

Dos opciones, según lo que hayas comprado:

**Módulo CSI (RPi Camera 3)** — mejor calidad y menos CPU:
1. Con la Pi apagada, conecta el cable plano al puerto CSI (contactos hacia el conector correcto; no lo fuerces).
2. En `config.yaml`: `backend: picamera`.
3. Comprueba: `libcamera-hello --list-cameras`.

**Webcam / mini cámara USB** — más simple de posicionar:
1. Enchúfala a un USB de la Pi.
2. En `config.yaml`: `backend: usb` y `device: /dev/video0`.
3. Comprueba: `ls /dev/video*` y que tu usuario está en el grupo video (`groups admin`; si no: `sudo usermod -aG video admin`).

**Colocación**: la base del Vintage 20 (14,5 × 14,5 × 16 cm, con su placa dentro) **no tiene sitio para la Pi 4** — monta la Pi en una caja aparte (bajo la mesa, dentro de una caja decorativa) y lleva hasta el teléfono solo los hilos del gancho y el audio. La cámara debe ver la cara de quien habla: una mini cámara USB escondida en una lámpara, un marco de fotos o un centro de flores frente a la cabina es lo más limpio. Haz pruebas de encuadre con alguien sentado *y* de pie.

---

## 7. LED de grabación (opcional)

Se enciende mientras se graba — útil para que el equipo vea de un vistazo que todo funciona.

```
pin 11 (GPIO 17) ──── resistencia 220 Ω ──── ánodo LED (pata larga)
pin 9  (GND)     ─────────────────────────── cátodo LED (pata corta)
```

Configurable en `config.yaml` (`led_gpio: 17`; pon `0` para desactivarlo).

---

## 8. Botón de regrabar saludo (opcional, del upstream)

Un pulsador entre **GPIO 23 (pin físico 16)** y **GND (pin 9)** permite regrabar el mensaje de bienvenida sin abrir el panel web (`record_greeting_gpio: 23` en `config.yaml`; pon `0` si no lo montas). Si quieres, en vez de un pulsador nuevo puedes soldar esos dos hilos a una de las teclas del propio Brondi (mide con el multímetro qué par de pistas cierra la tecla elegida).

---

## 9. Pendrive de respaldo (opcional)

Formatea un pendrive en **exFAT o FAT32** y déjalo enchufado. Cada sesión (`.wav` + `.mp4` + miniatura) se copia automáticamente a la carpeta `time-capsule-cam/` del pendrive al colgar. Sin configuración; si no hay pendrive, simplemente no se copia.

---

## 10. Alimentación y temperatura

- Usa la fuente oficial de 5 V/3 A. Cámara USB + codificación de vídeo + adaptador de audio suman; una fuente floja provoca cuelgues aleatorios (rayo amarillo en pantalla o `vcgencmd get_throttled` ≠ `0x0`).
- Pon el disipador **antes** de cerrar la caja de la Pi. El día del evento, vigila la temperatura de vez en cuando: `vcgencmd measure_temp` (objetivo < 70 °C).
- Con el modelo de 1 GB: durante el evento usa el panel solo para mirar el badge y reproducir alguna grabación — el botón **"Download All"** monta el ZIP en RAM y puede saturarla (la swap que configura el instalador lo amortigua, pero mejor no tentarlo). Para llevarte todo al final: `scp -r admin@<IP>:.../recordings/ .` o el pendrive de respaldo.
- La Pi va fuera del teléfono (ver sección 6), así que dale a su caja alguna vía de aire.

---

## 11. Checklist final de hardware

Con todo cableado y el software instalado ([README](../README.md)):

- [ ] `aplay -l` muestra el adaptador de audio USB
- [ ] `arecord`/`aplay` de prueba: se oye y se graba por el auricular del Brondi
- [ ] Descolgar el auricular reproduce el saludo y el pitido por el auricular
- [ ] `ls /dev/video*` muestra la cámara (backend usb) o `libcamera-hello --list-cameras` la detecta (backend picamera)
- [ ] **Con ambos servicios corriendo**, descolgar hace reaccionar a los dos: suena el saludo *y* `status.json` pasa a `recording`. Si el sidecar falla con "GPIO busy", los dos lectores del pin no conviven en tu imagen — revisa la nota del README
- [ ] Si descolgado/colgado sale invertido: `invert_hook: true` en `config.yaml`, no hay que resoldar
- [ ] El LED se enciende al descolgar y se apaga al colgar
- [ ] Colgar genera `.wav` + `.mp4` con marcas de tiempo cercanas en `/recordings/`
- [ ] Con pendrive puesto, la sesión aparece copiada en `<pendrive>/time-capsule-cam/`
- [ ] `vcgencmd measure_temp` < 70 °C tras 10 min grabando en bucle
- [ ] Prueba de encuadre: una grabación real, revisada en el panel web, con alguien usando el teléfono de forma natural
