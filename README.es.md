# radiobeacon — CD3DXZ-1

*English version: [README.md](README.md).*

**CD3DXZ-1** es una baliza experimental de propagación en VHF (banda de
2 m). Es una estación de radio, no solo un programa: un nodo de
radioafición —antes una pasarela EchoLink— que ahora transmite de forma
automática, en una frecuencia fija, para que otros operadores usen su
señal para comprobar si el camino en 2 m entre su estación y esta está
abierto.

En vez de emitir solo un identificador, la baliza pone *contenido útil* en
el aire: recoge comunicados de interés público (alertas tempranas de
protección civil, reportes sísmicos), convierte cada uno en una
transmisión corta y la emite por el canal (repetidora o simplex) cuando la
frecuencia está libre. Quien copia la baliza obtiene a la vez una prueba
de propagación y el comunicado en sí.

Todo lo que hay en este repositorio es la automatización detrás de esa
estación: la recolección de datos, la preparación del texto y la entrega
al aire. Se asume que el transmisor, la interfaz de audio y el propio
SvxLink ya están instalados; su despliegue queda fuera del alcance de este
repositorio.

---

## La estación

| | |
|---|---|
| **Indicativo** | CD3DXZ-1 |
| **Banda** | VHF, 2 m (144–146 MHz) |
| **Modo** | El operador elige **uno**: **voz** hablada, o **paquete AX.25** (AFSK 1200 baudios) |
| **Frecuencia / localizador** | Se define en cada instalación (identidad de la estación, se ingresa en el panel) |
| **Control del transmisor** | [SvxLink](https://www.svxlink.org/) — controla el PTT y reproduce cada clip solo cuando el canal está libre |
| **Servicio** | Deshabilitado por defecto; el operador habilita la transmisión de forma explícita |
| **Identificación** | El indicativo se emite (hablado o en el frame) con cada comunicado; queda pendiente un ID en CW continuo, exigido por la normativa chilena |

La baliza transmite **un solo tipo de contenido a la vez**. Cambiar entre
voz y paquete es un cambio de configuración, no una reinstalación: la cola
de transmisión pendiente del otro tipo se limpia automáticamente.

### Voz

Cada comunicado se locuta en español con un motor de texto a voz sin
conexión (Piper, neuronal; `espeak-ng` como alternativa). El texto hablado
se envuelve en un formato fijo de boletín —nombre de la fuente, fecha y
una frase de cierre que remite a las fuentes oficiales— para que, incluso
un comunicado truncado, se entienda como un aviso informativo y no se
confunda con un canal oficial.

### Paquete AX.25

Cada comunicado se envía como un frame UI de AX.25 a 1200 baudios AFSK,
generado como audio con la herramienta `gen_packets` de Direwolf (sin TNC
en ejecución, sin conexión de red). El frame usa un formato de mensaje
compatible con APRS (`INDICATIVO>DESTINO:texto`) **pero no se transmite en
la frecuencia de llamada de APRS**: opera en una frecuencia experimental
coordinada, de modo que nunca aparece en la red pública de APRS. Los
comunicados largos se parten en varios frames; la longitud del frame se
mantiene automáticamente bajo el límite de ~256 bytes de AX.25.

---

## Qué sale al aire

El contenido proviene de **fuentes** configurables. Cada fuente se
consulta en su propio intervalo, los ítems nuevos se almacenan, se acortan
de forma opcional y luego se encolan para transmisión.

- **Alertas tempranas de SENAPRED** — alertas del sistema de alerta
  temprana de protección civil de Chile (eventos meteorológicos,
  hidrológicos y geofísicos).
- **Reportes sísmicos del CSN** — sismos publicados por el Centro
  Sismológico Nacional.
- **Cualquier feed HTTP/JSON** — se agregan otras fuentes (APIs de clima,
  lecturas de sensores locales, etc.) desde el panel, describiendo el
  endpoint y cómo mapear sus campos; sin tocar código.

Toda transmisión de tipo alerta se marca explícitamente como una
**retransmisión experimental y no oficial** y remite a SENAPRED como
fuente autoritativa.

### Del ítem a la transmisión

```
fuentes  →  ítem almacenado  →  (opcional) resumen por IA, 2 o 3 oraciones  →  ajustado al modo elegido
                                                                                  │
                                                                    encolado: N transmisiones,
                                                                    cada M segundos
                                                                                  │
                                                          renderizado a un WAV, de a uno
                                                                                  │
                                        entregado a SvxLink → se emite cuando el canal está libre
```

### El rol de la IA

La IA cumple aquí una única función acotada, y está **deshabilitada por
defecto**: condensar un comunicado para que quepa en una transmisión al
aire. Un ítem de una fuente suele ser un aviso web completo — demasiado
largo para locutarlo en una ventana razonable o para caber en un frame de
paquete. Cuando se habilita, cada ítem nuevo se envía a un modelo de
lenguaje con un prompt fijo y editable por el operador (en español,
«resume este aviso en 2 o 3 oraciones claras y completas, sin inventar
nada, sin dejar oraciones a medias»). El resultado se guarda como el
`summary` del ítem y es lo que usan luego las etapas de voz y paquete; el
texto original queda intacto.

- **Independiente del proveedor** — OpenAI, Claude, o un modelo Ollama
  autoalojado, según la configuración. Es el único paso que hace una
  llamada saliente a un servicio externo; con Ollama es completamente
  local.
- **Solo cuando aporta** — los ítems ya lo bastante cortos pasan directo,
  nunca se envían a un proveedor.
- **Sin reescritura silenciosa ante fallos** — si la llamada al modelo
  falla, el ítem *no* se transmite, en lugar de salir al aire con texto
  parcial o sin revisar. Una fuente puede optar por caer de vuelta a su
  título simple (SENAPRED lo hace).
- **No es un filtro ni toma decisiones** — nunca elige qué se transmite,
  ni cambia una política de transmisión, ni edita la identidad de la
  estación. Solo acorta texto que una fuente definida por una persona ya
  seleccionó.

La salida del modelo no se recorta a posteriori por longitud —el prompt
pide un resumen corto y completo—, así que un modelo que se porte mal
todavía queda atajado por los límites de longitud de cada modo, más
abajo.

### Repetición

Cada ítem se pone al aire una cantidad fija de veces, con un intervalo
fijo entre repeticiones: esos dos números son una **política de
transmisión** con nombre. Las políticas se editan desde el panel; el
operador puede sobreescribir la política de un ítem puntual, o rearmar un
ítem que ya terminó sus repeticiones. Cada intento cuenta para el total,
de modo que un comunicado no se transmite indefinidamente si el enlace
falla. La cola se guarda en disco y sobrevive a un reinicio.

---

## Operación

El panel (página web local, `http://127.0.0.1:8080`) es la consola del
operador:

- estado actual de la baliza — habilitada/deshabilitada, modo activo,
  transmisiones pendientes, último latido, desviación de reloj medida por
  NTP;
- ítems recientes y la bitácora de auditoría al aire (qué se transmitió,
  cuándo y con qué resultado);
- habilitar/deshabilitar la transmisión y cambiar de modo sin reiniciar;
- explorar/buscar ítems, sobreescribir una política de transmisión,
  rearmar un ítem;
- administrar fuentes y políticas de transmisión.

El reloj lo disciplina el cliente NTP del propio sistema operativo. La
baliza, además, mide su desviación de reloj contra un servidor NTP público
solo para mostrarla; nunca ajusta el reloj por sí misma.

---

## Ejecutar la automatización

```bash
./start.sh   # clon nuevo: crea .env, prepara un venv compartido
             # y arranca junto la recolección de datos + la entrega + las
             # acciones + el panel + la baliza (Ctrl+C detiene todo).
             # No requiere secretos; todos los valores por defecto son
             # seguros, y la baliza arranca deshabilitada (BEACON_ENABLED=false).

./query_history.sh --help   # consultas SQL ad-hoc contra la base local
./stop.sh --status          # qué está corriendo, y su pid
```

Con todo en marcha, abre `http://127.0.0.1:8080`.

Cada componente también puede ejecutarse por separado
(`./data-adapters/start.sh`, `./dispatcher/start.sh`, `./mq/start.sh`,
`./actions/start.sh`, `./ui/start.sh`, `./beacon/start.sh`) — cada uno es
autónomo y se niega a arrancar dos veces.

### Cómo está construido

Siete componentes desacoplados, conectados solo por una base de datos
SQLite compartida (`storage/radiobeacon.db`) y, de forma opcional, un
broker MQTT local. Cada uno tiene su propio README con el detalle.

```
data-adapters/  consulta fuentes externas, almacena ítems crudos     → data-adapters/README.md
dispatcher/     observa ítems nuevos y los entrega a los handlers     → dispatcher/README.md
mq/             broker MQTT local opcional (Docker)                   → mq/README.md
actions/        pipeline MQTT opcional (resumen IA, troceo, …)        → actions/README.md
ui/             el panel del operador (FastAPI, renderizado servidor) → ui/README.md
beacon/         la capa de transmisión — renderiza y entrega a SvxLink → beacon/README.md
storage/        la base de datos SQLite compartida                    → storage/README.md
```

`beacon/` renderiza cada ítem encolado a un único WAV y lo deja en la
carpeta *spool* de `svxlink-txqueue`; SvxLink lo reproduce en el siguiente
canal libre. Desplegar SvxLink y `svxlink-txqueue` queda fuera del
alcance — ver
[documentation/svxlink-txqueue-SETUP.md](documentation/svxlink-txqueue-SETUP.md).
[CONTEXT.md](CONTEXT.md) registra el diseño original de la estación (el
esquema TDMA de voz/paquete intercalados que `beacon/` luego reemplazó por
el modelo más simple de un tipo a la vez).

---

## Configuración

Toda la configuración es un único archivo `.env` en la raíz del repo
(copia `.env.example` a `.env`). La mayoría de los ajustes también se
editan en vivo desde la página `/config` del panel. Las variables propias
de un componente o fuente llevan su ruta como prefijo
(`ADAPTERS_SENAPRED_*`, `BEACON_*`); las que lee directamente un SDK de
terceros conservan el nombre propio de ese SDK.

La **identidad de la estación** (indicativo, descripción, localizador,
frecuencia, contacto del operador) se ingresa solo en el panel y nunca se
lee del entorno: un `BEACON_CALLSIGN` perdido en la shell no puede poner
un indicativo equivocado al aire. La baliza se niega a transmitir mientras
falte algún campo de identidad.

## La hora siempre es UTC

Toda fecha/hora que se maneja en cualquier parte de este repo es UTC con
zona horaria explícita, sin excepciones en almacenamiento, planificación
de transmisión ni cálculo interno. El único lugar donde aparece una zona
local es el texto mostrado a una persona —fechas de los comunicados
hablados, el panel, logs legibles— que se convierte a `DISPLAY_TIMEZONE`
(por defecto `America/Santiago`) en el último paso y nunca se reintroduce
en nada almacenado. Ver los comentarios de código en
`adapters/timeutil.py` para la regla completa.
