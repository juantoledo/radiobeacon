# Contexto del proyecto: Baliza Experimental de Propagación — para Claude Code

> Este documento resume una conversación de diseño previa (en claude.ai) para que
> Claude Code tenga el contexto completo y pueda empezar a generar/estructurar
> el repo sin tener que re-derivar las decisiones ya tomadas.

## Objetivo

Convertir un nodo que antes operaba como EchoLink en una **baliza experimental
de propagación de radioafición**, que combina:

- Reportes periódicos por **voz** (SvxLink).
- Ventanas de **datos digitales AX.25** intercaladas (Direwolf), usadas como
  ping/pong entre balizas de distintos operadores.
- Sincronización de tiempo por **NTP**, con disciplina tipo TDMA (inspirado en
  WSPR/FT8/NCDXF-IARU beacon network).
- Contenido dinámico: alertas SENAPRED, clima, telemetría de sensores.

(La capa de datos ya implementada en este repo sigue una regla propia de
consistencia horaria — ver "Dates and times: always UTC" en
[README.md](README.md) — independiente de la disciplina NTP/TDMA de la
capa de radio descrita más abajo.)

## Hardware confirmado y ya probado

- Transceptor operativo, antena instalada.
- Interfaz de audio: **USB PnP Sound Device** (`card 0` en ALSA, `plughw:0,0`).
- PTT: confirmado funcionando vía **CM108** (`/dev/hidraw0`, GPIO 3) — en algún
  momento también se detectó una config alternativa apuntando a
  `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0` (adaptador CH340); requiere
  que el usuario esté en el grupo `dialout` para ese caso.
- Mini PC (no solo Raspberry Pi) — permite correr Docker Compose con varios
  servicios simultáneos.
- Usuario del sistema: `jtoledoc`, host `shack`.

## Decisiones de arquitectura tomadas

> **Nota (actualización posterior):** el diseño TDMA descrito en esta sección
> (slots de voz/AX.25 intercalados, orquestador de *timing*, arbitraje de la
> tarjeta de audio entre SvxLink y Direwolf) fue **reemplazado** en `beacon/`
> por un modelo más simple: el operador elige **un** tipo de baliza
> (`BEACON_TYPE` = `voice` o `frame`), cada ítem se renderiza a **un archivo
> WAV** (TTS para voz; `gen_packets` de Direwolf para AX.25) y ese WAV se deja
> en la carpeta *spool* de `svxlink-txqueue` para que SvxLink lo emita cuando el
> canal esté libre. Ya no hay ventana TDMA, ni proceso Direwolf en ejecución, ni
> contención de audio. Ver [beacon/README.md](beacon/README.md) y
> [documentation/svxlink-txqueue-SETUP.md](documentation/svxlink-txqueue-SETUP.md).
> El texto que sigue se conserva como registro del diseño original.

### Separación de responsabilidades (importante — no mezclar capas)

1. **Data sources / adapters** — obtienen datos crudos, no saben nada de radio.
   - SENAPRED (alertas tempranas) — sin API pública documentada, requiere
     scraping. Ver `senapred.cl` y el proyecto de terceros `4drr.com` (ya
     estructura datos de SENAPRED, revisar si expone algo consumible).
   - Clima (API externa, ej. OpenWeatherMap, o DMC si aparece algo estructurado).
   - Sensores locales (temperatura, voltaje) vía el propio mini PC.
   - Resultado del último ping AX.25 (fuente interna, no externa).
2. **Agregador de estado** — consolida todas las fuentes en un único snapshot
   (fuente única de verdad), lo persiste (Postgres/SQLite). NO decide qué se
   transmite ni cómo — solo consolida.
3. **Formatters (uno por canal)** — a partir del mismo estado, generan:
   - `voice_formatter` → texto natural en español para TTS.
   - `ax25_formatter` → payload corto (formato tipo APRS `:INDICATIVO :msg{seq}`),
     pensado para que quepa en el slot de datos corto.
4. **TTS engine** — texto → audio (wav). Función pura, sin lógica de negocio.
5. **Radio TX layer**:
   - `svxlink_control` — dispara reproducción de audio ya generado.
   - `ax25_control` — habla con el socket **KISS TCP de Direwolf** (puerto 8001).
6. **Orquestador (TDMA)** — el único componente con lógica de *timing*. No sabe
   de contenido, solo coordina cuándo llamar a cada pieza. Ya existe un
   esqueleto funcional (ver más abajo).
7. **Resource arbiter** — resuelve la contención de la tarjeta de audio entre
   SvxLink y Direwolf (ver sección de hallazgos técnicos).

### Esquema de tiempo (TDMA, valores ilustrativos, a validar)

```
segundo 0   → inicio slot de VOZ
segundo 35  → fin de VOZ
segundo 35-37 → guard time
segundo 37  → inicio slot AX.25 (ping/pong)
segundo 50  → fin de slot AX.25
segundo 50-60 → silencio / reset
```

Loop cerrado entre slots: el slot de voz de cada ciclo narra el resultado del
AX.25 del ciclo anterior (RTT, packet loss), dándole propósito real al reporte
de voz más allá de la identificación.

### Compatibilidad con AX.25 / APRS

- Se usa **AX.25 UI frames a 1200 baud AFSK**, formato de mensaje compatible
  con APRS (`ORIGEN>DESTINO:contenido`), pero **NO en la frecuencia estándar
  de APRS** — se coordinará una frecuencia experimental propia con el otro
  operador, precisamente para no interferir ni aparecer en APRS.fi.
- El campo `DESTINO` (tocall) no enruta nada — es solo un identificador de
  software/proyecto. Decisión tomada: usar destinos propios como `WXALRT` o
  `EXPTL` en vez de `APRS`, para dejar claro que es tráfico experimental, no
  parte de la red APRS pública.
- Frecuencia del canal experimental: **pendiente de coordinar** con el
  segundo operador (define separación respecto a la repetidora en uso y
  filtrado necesario en recepción).

### Formato de ejemplo de alerta ya probado (AX.25, vía kissutil)

```
N0CALL-1>WXALRT:ALERTA TEMPRANA - Lluvias intensas previstas Region de Coquimbo, acumulados 40-60mm en 24h, riesgo de anegamientos sectores bajos y crecidas de quebradas, vigencia 19-21 Ago, fuente experimental no oficial, consulte SENAPRED
```
(~240 caracteres — válido pero grande para el slot corto; en producción
conviene comprimir mucho más, o partir en varios frames).

Límite técnico de AX.25 UI frame: ~256 bytes de payload (algunas
implementaciones toleran hasta ~300-330).

## Infraestructura: Docker Compose

Decisión: **todo el stack corre en Docker Compose** en el mini PC — tanto lo
construido a medida (orquestador, formatters, adapters) como lo reusado
(SvxLink, Direwolf, n8n, Postgres, Grafana).

### Servicios definidos

- `direwolf` — requiere Dockerfile propio (no hay imagen oficial), compilado
  desde `github.com/wb2osz/direwolf`. Necesita `devices: /dev/snd`,
  `/dev/hidraw0` (o `/dev/ttyUSB0` según el caso), grupos `audio`+`dialout`.
  Expone KISS TCP en 8001 y AGW en 8000.
- `svxlink` — requiere Dockerfile propio, compilado desde
  `github.com/sm0svx/svxlink`. Mismos requisitos de `devices`/grupos que
  Direwolf. **Compite por el mismo hardware de audio** (ver hallazgo crítico
  abajo).
- `orchestrator` — el script Python propio (TDMA + formatters + adapters).
  NO necesita `devices:` — habla con Direwolf/SvxLink por red interna
  (socket KISS / comando remoto), no toca hardware directo.
- `n8n` — cubre las fuentes de datos tolerantes a latencia (SENAPRED, clima),
  vía nodos HTTP Request + scheduling nativo. Corre cada 10-15 min. Escribe
  el resultado a Postgres, que el agregador del orquestador lee.
- `postgres` — persistencia del estado consolidado y de telemetría histórica.
- `grafana` — dashboards (RTT del ping AX.25 en el tiempo, packet loss por
  hora, telemetría de sensores, uptime, timeline de alertas disparadas).

### Hallazgo crítico: contención de tarjeta de audio (YA CONFIRMADO EN PRUEBAS REALES)

SvxLink y Direwolf **no pueden abrir `/dev/snd/pcmC0D0c` al mismo tiempo**
(error real reproducido: `Device or resource busy`, confirmado con
`fuser -v /dev/snd/*` mostrando el PID de `svxlink` reteniendo el device en
modo full-duplex exclusivo). Este problema existe tanto corriendo nativo como
dockerizado — Docker no lo resuelve, solo pasa el device crudo tal cual.

Tres estrategias evaluadas (ninguna implementada aún, pendiente de decidir):

1. **Parar/arrancar por slot** — el orquestador para SvxLink antes del slot
   AX.25 y arranca Direwolf, y viceversa. Simple pero con latencia de
   arranque/parada de cada servicio a medir y compensar.
2. **ALSA `dmix`/`dsnoop`** (recomendado) — capa de ALSA a nivel del **host**
   (`/etc/asound.conf`), define un dispositivo virtual (ej. `dbeacon`) que
   multiplexa el acceso. Ambos programas (nativos o en containers) apuntan a
   ese device virtual en su `ADEVICE`/config de audio, en vez del hardware
   crudo. Debe configurarse en el host ANTES de que Docker entre en escena —
   no viaja con los containers, hay que replicarlo en cada host nuevo.
3. **Segunda interfaz de audio USB** dedicada — la más simple conceptualmente,
   elimina la contención de raíz, pero requiere hardware adicional (~10-15 USD).

### Script de aprovisionamiento (esqueleto ya definido)

Se definió (no verificado en el host real aún) un script bash de
aprovisionamiento que:
1. Instala Docker + Docker Compose plugin si faltan.
2. Detecta la tarjeta de audio real vía `arecord -l` (¡ojo!: la detección
   "primera tarjeta" es ingenua — si el host tiene más de una tarjeta,
   conviene filtrar por nombre, ej. grep "USB").
3. Genera `/etc/asound.conf` con `dmix`/`dsnoop` apuntando al índice correcto.
4. Agrega el usuario a los grupos `audio`, `dialout`.
5. Verifica que exista un dispositivo PTT en `/dev/serial/by-id/`.
6. Deja advertencia de que hay que recerrar sesión para que los grupos
   apliquen antes de continuar con el build/deploy.

## Hallazgos de troubleshooting reales (para no repetir)

Ya diagnosticados y resueltos en esta sesión, en este orden:

1. `plughw:0.0` (con punto) → error de sintaxis ALSA, corregido a `plughw:0,0`
   (con coma).
2. `Cannot get card index for 0` → causa real: Direwolf corriendo como
   **root** mientras la sesión de audio pertenecía al usuario `jtoledoc`
   (con PulseAudio/PipeWire de sesión de usuario de por medio). Solución:
   correr Direwolf como `jtoledoc`, no como root.
3. `Device or resource busy` → causa real: **SvxLink ya tenía la tarjeta
   abierta** (confirmado con `fuser -v /dev/snd/*`, PID de `svxlink`).
   Solución temporal: `sudo systemctl stop svxlink` antes de correr Direwolf.
   Solución permanente: pendiente (ver sección dmix/dsnoop arriba).
4. Con SvxLink detenido, Direwolf levantó correctamente: modem AFSK 1200 baud
   configurado, PTT vía `/dev/hidraw0` GPIO 3, puertos AGW (8000) y KISS TCP
   (8001) escuchando.
5. Luego apareció una config alternativa con `ERROR can't open device
   /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 ... Permission denied` →
   causa: el device pertenece al grupo `dialout`, usuario no estaba en ese
   grupo. Solución: `sudo usermod -aG dialout jtoledoc` + relogin (o
   `newgrp dialout` para probar rápido).
6. Envío de frames de prueba validado con `kissutil -p 8001`, formato TNC2:
   `ORIGEN>DESTINO:contenido`. Probado con mensajes cortos y uno largo (~240
   caracteres) sin problemas — dentro del límite de AX.25.

## Referencias de diseño (para no reinventar protocolo)

- **NCDXF/IARU Beacon Network** — slots de tiempo sincronizados, formato fijo
  de identificación al inicio de cada ventana.
- **WSPR / FT8** — disciplina de sincronización por reloj (NTP), sin
  negociación por radio.
- **NOAA SAME** — intercalado de datos digitales cortos dentro de flujo de voz
  (patrón replicado conceptualmente, no el formato exacto).
- **APRS** — formato estándar de mensajes de texto sobre AX.25.
- **ALE (MIL-STD-188-141)** — referencia conceptual para "sounding"
  automático, no implementado en esta fase.

Simplificaciones deliberadas respecto a los estándares originales: NTP
estándar (sin GPS/PPS), AX.25 en modo no conectado (UI frames, sin
retransmisión automática).

## No existe una plataforma única que cubra todo

Búsqueda ya realizada — no hay un proyecto en GitHub que replique este diseño
específico (voz + AX.25 intercalados en TDMA con orquestador propio). Piezas
sueltas relevantes: `wb2osz/direwolf` (beaconing/telemetry toolkit nativo,
ver `src/beacon.c`), `t1djoe/ax25-aprs-lib` (generador de frames AX.25/APRS
en C). Ninguna cubre la combinación completa — confirma que el orquestador,
los formatters y el pegamento de integración son la parte que hay que
construir a medida; SvxLink, Direwolf, n8n, Postgres y Grafana se reusan tal
cual.

## Archivos ya generados en la conversación previa

- `baliza-diseno.md` — documento de diseño completo (versión anterior a
  este resumen; este CONTEXT.md lo supera en algunos puntos, especialmente en
  hallazgos de hardware ya probados).
- `orquestador_baliza.py` — esqueleto inicial del orquestador TDMA en Python.
  Lógica de timing ya funcional (recalcula contra reloj real, no acumula
  sleeps). Puntos de integración con SvxLink/Direwolf marcados con `TODO`.
  Incluye placeholders `PTT_LATENCY_S` y `TNC_LATENCY_S` pendientes de medir
  en el hardware real.

## Próximos pasos sugeridos (orden de prioridad)

1. Coordinar frecuencia experimental con el otro operador (bloqueante para
   pruebas en el aire).
2. Decidir y aplicar la estrategia de convivencia de audio entre SvxLink y
   Direwolf (recomendado: `dmix`/`dsnoop` a nivel host).
3. Completar los `TODO` del orquestador (`run_voice_slot`, `run_ax25_slot`)
   con la integración real a SvxLink (TCL events o comando remoto) y al
   socket KISS de Direwolf.
4. Medir latencia real de PTT/arranque de TNC en el hardware, ajustar
   `PTT_LATENCY_S`/`TNC_LATENCY_S`.
5. Armar los Dockerfiles de Direwolf y SvxLink (compilación desde fuente,
   multi-stage build).
6. Armar `docker-compose.yml` completo (direwolf, svxlink, orchestrator, n8n,
   postgres, grafana) con los `devices:`/`group_add:` correctos.
7. Definir el esquema de base de datos (tabla de estado consolidado, tabla de
   histórico de pings/telemetría) que conecta n8n → Postgres → orquestador →
   Grafana.
8. Confirmar si existe endpoint estructurado detrás del mapa de senapred.cl o
   meteochile.gob.cl (inspección de Network tab del navegador) antes de
   decidir scraping vs API real.
9. Especificar formato final y compacto del payload AX.25 para producción
   (el ejemplo de ~240 caracteres probado es válido pero muy largo para el
   slot corto real).

## Notas de identificación / regulatorias

- Indicativo: se define en cada instalación.
- Mantener identificación en CW según normativa del país (Chile).
- Dejar explícito en cualquier contenido tipo alerta que es "fuente
  experimental, no oficial" y remitir a SENAPRED como fuente real.
