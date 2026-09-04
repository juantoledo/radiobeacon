# svxlink-txqueue — transmitir archivos WAV pregrabados por SvxLink

Un pequeño servicio que vigila una carpeta, encola cualquier `*.wav` que se
deposite en ella y reproduce cada uno al aire a través de una lógica SvxLink
existente — de uno en uno, solo cuando el canal de RF está libre.

- **Sin conversión a repetidor, sin receptor extra, sin trucos de tarjeta de sonido.**
- Funciona con una lógica estándar `TYPE=Simplex` (o `Repeater`).
- Python 3 puro de la biblioteca estándar — no hace falta `ffmpeg`, `sox` ni `audioop`.

Probado con SvxLink `1.9.1@25.05.1`, Python 3.14, systemd de la familia Ubuntu.

---

## 1. Cómo funciona

Las lógicas de SvxLink exponen un **`COMMAND_PTY`** opcional — un pseudo-tty al
que el SO puede escribir comandos. Uno de sus comandos es:

```
EVENT <tcl-proc> [args...]
```

que invoca un manejador de eventos TCL en la lógica. El manejador integrado
`::playFile` activa el transmisor, reproduce un archivo de audio a través del
manejador de mensajes de la lógica y desactiva el transmisor — exactamente lo
que SvxLink hace para las identificaciones de voz horarias.

Así que toda la funcionalidad es:

```
incoming/*.wav ──► queue/ ──► convertir a WAV mono 16 kHz ──► staging/
                                        │
                     esperar a que el canal esté libre
                                        │
        echo 'EVENT ::playFile /…/staging/clip.wav' > $COMMAND_PTY
                                        │
             SvxLink activa TX, reproduce el clip, desactiva
                                        │
                          mover el original ──► sent/
```

### ¿Por qué no "inyectarlo como audio recibido"?

Una lógica `TYPE=Simplex` **nunca retransmite el audio recibido**. Alimentar un
WAV a un receptor falso (dispositivo de audio UDP, loopback ALSA, etc.) abre el
squelch pero el transmisor sigue apagado. Solo la identificación / respuestas
DTMF / módulos / **anuncios encolados por TCL** producen audio de transmisión en
una lógica simplex. `COMMAND_PTY` + `EVENT ::playFile` es la ruta de anuncios,
gobernada desde el exterior.

Una lógica `TYPE=Repeater` *sí* repite el audio de RX, pero aun así no conviene
falsear un receptor solo para anuncios — `COMMAND_PTY` también funciona ahí, de
forma idéntica.

---

## 2. Requisitos previos

- SvxLink instalado y en ejecución con al menos una lógica que tenga un
  **transmisor** (`TX=...`). Esta guía asume que la sección de lógica se llama
  `[SimplexLogic]` — ajusta el nombre en todas partes si el tuyo difiere.
- El servicio SvxLink se ejecuta como un usuario dedicado (comúnmente `svxlink`).
  Averígualo:
  ```sh
  systemctl show svxlink -p User --value        # o revisa /etc/default/svxlink
  ```
  Todo lo que sigue usa `svxlink` — sustituye por tu valor.
- `python3` ≥ 3.8.
- Acceso de escritura para el usuario de SvxLink al directorio donde se crea el
  enlace simbólico `COMMAND_PTY` (por defecto `/dev/shm`, que es escribible por
  todos — está bien).

---

## 3. Instalación

### 3.1 Configuración de SvxLink

Edita `/etc/svxlink/svxlink.conf`, en la sección `[SimplexLogic]`, añade una línea:

```ini
[SimplexLogic]
TYPE=Simplex
RX=Rx1
TX=Tx1
# PTY de control: svxlink-txqueue escribe aquí "EVENT ::playFile <abs-wav>" para
# activar el transmisor y reproducir un clip pregrabado.
COMMAND_PTY=/dev/shm/svxlink_simplex_ctrl
```

Nada más en `svxlink.conf` cambia. Fíjate en el valor `TIMEOUT` de la lógica
(por defecto `300` segundos) — limita cuánto puede durar una sola transmisión,
así que limita la duración del clip. El servicio rechaza clips más largos que
`TIMEOUT − 5 s`.

> Si el panel de radiobeacon corre en este host, esta línea también se puede
> agregar desde **/config → Beacon — SvxLink** (activa antes
> `BEACON_RF_CONF_EDITOR_ENABLED` — viene apagado porque el panel no tiene
> login). Escribe un `.bak` con fecha y no reinicia SvxLink; ejecuta
> `systemctl restart svxlink` tú después.

Reinicia SvxLink y confirma que el PTY aparece:

```sh
systemctl restart svxlink
ls -l /dev/shm/svxlink_simplex_ctrl        # -> enlace simbólico a /dev/pts/N, propiedad del usuario svxlink
```

### 3.2 Directorios de spool

```sh
for d in incoming queue staging sent failed; do
    install -d -o svxlink -g svxlink -m 2775 "/var/spool/svxlink-tx/$d"
done
```

`incoming/` es `2775` (escribible por el grupo, setgid) para que los operadores
del grupo `svxlink` puedan depositar archivos por SFTP sin `sudo`. Añade un
usuario a ese grupo con:

```sh
usermod -aG svxlink <username>
```

### 3.3 El demonio

Guárdalo como `/usr/local/bin/svxlink-txqueue`, luego hazle `chmod +x`. Script
completo en el [Apéndice A](#apéndice-a--usrlocalbinsvxlink-txqueue).

```sh
install -m 0755 svxlink-txqueue /usr/local/bin/svxlink-txqueue
python3 -c "import ast; ast.parse(open('/usr/local/bin/svxlink-txqueue').read())" && echo OK
```

### 3.4 La unidad de systemd

Guárdala como `/etc/systemd/system/svxlink-txqueue.service` (texto completo en
el [Apéndice B](#apéndice-b--etcsystemdsystemsvxlink-txqueueservice)), luego:

```sh
systemctl daemon-reload
systemctl enable --now svxlink-txqueue.service
systemctl status svxlink-txqueue.service
```

---

## 4. Uso

Deposita un WAV en la carpeta vigilada:

```sh
cp announcement.wav /var/spool/svxlink-tx/incoming/
```

- Se acepta cualquier WAV PCM: 8/16/24/32 bits, mono o estéreo, cualquier
  frecuencia de muestreo. Se mezcla a mono y se remuestrea a 16 kHz (la
  frecuencia interna de SvxLink).
- El archivo se recoge una vez que deja de crecer (2 s sin cambios), se encola
  con un prefijo de marca temporal (para que el procesamiento sea FIFO estricto),
  se transmite cuando el canal está libre y luego se mueve a `sent/`.
- Ante cualquier error el archivo va a `failed/` con un archivo adjunto
  `<nombre>.log` que contiene el motivo / traza.

Míralo funcionar:

```sh
journalctl -fu svxlink-txqueue
tail -f /var/log/svxlink        # en otra terminal
```

### Parámetros ajustables (`Environment=` de systemd o un drop-in)

| Variable | Por defecto | Significado |
|---|---|---|
| `TXQUEUE_SPOOL` | `/var/spool/svxlink-tx` | raíz del spool |
| `TXQUEUE_COMMAND_PTY` | `/dev/shm/svxlink_simplex_ctrl` | debe coincidir con `svxlink.conf` |
| `TXQUEUE_SVXLINK_LOG` | `/var/log/svxlink` | log del que se lee el estado del canal |
| `TXQUEUE_TARGET_RATE` | `16000` | frecuencia de remuestreo objetivo (frecuencia interna de SvxLink) |
| `TXQUEUE_STABLE_SECONDS` | `2.0` | tiempo de inactividad antes de encolar un archivo |
| `TXQUEUE_IDLE_GUARD` | `3.0` | el canal debe estar libre este tiempo antes de TX |
| `TXQUEUE_IDLE_MAX_WAIT` | `600` | dejar de esperar a que quede libre, transmitir igualmente |
| `TXQUEUE_TX_ON_TIMEOUT` | `20` | fallar si el TX no se activa dentro de este tiempo tras el disparo |
| `TXQUEUE_TX_OFF_MARGIN` | `15` | holgura añadida a la duración del clip al esperar el TX-off |
| `TXQUEUE_SVX_TX_TIMEOUT` | `295` | rechazar clips más largos que esto (mantener < `TIMEOUT` de la lógica) |

Ejemplo de drop-in:

```sh
mkdir -p /etc/systemd/system/svxlink-txqueue.service.d
cat > /etc/systemd/system/svxlink-txqueue.service.d/local.conf <<'EOF'
[Service]
Environment=TXQUEUE_IDLE_GUARD=5
Environment=TXQUEUE_COMMAND_PTY=/dev/shm/my_logic_ctrl
EOF
systemctl daemon-reload && systemctl restart svxlink-txqueue
```

---

## 5. Verificación

> Los pasos 3–5 activan el transmisor real. Usa una carga fantasma o una
> frecuencia simplex despejada, e identifícate según tu licencia.

1. **La configuración carga** — `systemctl restart svxlink`; `/var/log/svxlink`
   muestra la lógica y su RX/TX cargando sin errores;
   `ls -l /dev/shm/svxlink_simplex_ctrl` es un enlace simbólico.
2. **El demonio arranca** — `systemctl restart svxlink-txqueue`; el journal
   muestra `svxlink-txqueue started (... command_pty=... rate=16000)`;
   `staging/` existe.
3. **Prueba manual del PTY** (ejecuta como el usuario de SvxLink — incluso root
   recibe `EACCES` sobre el pts esclavo de otro usuario):
   ```sh
   sudo -u svxlink sh -c \
     "printf 'EVENT ::playFile /usr/share/svxlink/sounds/en_US/Core/online.wav\n' > /dev/shm/svxlink_simplex_ctrl"
   ```
   `/var/log/svxlink` → `Turning the transmitter ON` … `OFF`, audio al aire.
4. **Ruta completa** — `cp test.wav /var/spool/svxlink-tx/incoming/`; el journal
   muestra `queued → transmitting (N.Ns) → done`; `Tx1: Turning the transmitter
   ON/OFF` en el log de SvxLink; el archivo aterriza en `sent/`; `staging/` está
   vacío de nuevo.
5. **Ruta de fallo** — `printf 'not audio' > /var/spool/svxlink-tx/incoming/bad.wav`
   → termina en `failed/` con un archivo adjunto `.log`.

---

## 6. Resolución de problemas

| Síntoma | Causa / solución |
|---|---|
| Archivo encolado, `transmitter never keyed within Ns` → `failed/` | Discrepancia en el nombre de `COMMAND_PTY` entre `svxlink.conf` y el entorno del servicio; o SvxLink no está en ejecución; o `::playFile` recibió una ruta incorrecta — revisa `/var/log/svxlink` en busca de `*** ERROR`. |
| `<pty> missing - svxlink not running` | SvxLink caído, o `COMMAND_PTY` no está definido en la sección de lógica, o está definido en la lógica equivocada. |
| `write to <pty> failed: [Errno 5/6]` | Enlace simbólico obsoleto tras un fallo de SvxLink — `systemctl restart svxlink` lo recrea. |
| Audio truncado | Clip más largo que el `TIMEOUT` de la lógica; SvxLink cortó la transmisión. Reduce la duración del clip o aumenta `TIMEOUT`. |
| No transmite nada, `idle wait timed out` nunca aparece, el journal en silencio | El canal nunca queda libre (squelch atascado). Revisa el receptor; el servicio espera hasta `TXQUEUE_IDLE_MAX_WAIT` y luego transmite igualmente. |
| Audio distorsionado / a velocidad incorrecta | El WAV de origen es float o ADPCM, no PCM — `wave` solo lee PCM. Vuelve a exportarlo como WAV PCM. |
| Permiso denegado al escribir en `incoming/` por SFTP | Añade al operador al grupo `svxlink` y que vuelva a iniciar sesión. |
| Una ruta de `EVENT` con espacios falla | El parser del PTY separa por espacios en blanco e ignora las comillas. El servicio ya sanea los nombres en staging a `[A-Za-z0-9._-]`; solo relevante si invocas el PTY a mano. |

### Notas de comportamiento

- Un clip reproducido cuenta como un anuncio de la lógica: el pitido de roger
  suena después y los temporizadores de identificación se reinician, igual que
  con cualquier otro anuncio.
- `MUTE_TX_ON_RX` (activado por defecto) hace que el propio SvxLink aplace el
  anuncio si el canal se abre entre el disparo y el TX; la espera de canal libre
  del servicio normalmente evita que eso importe.
- La finalización se detecta leyendo `/var/log/svxlink` en busca de
  `Turning the transmitter ON` y luego un `OFF` estable, acotado por
  `duración del clip + TX_OFF_MARGIN`. Si una identificación activa el TX más o
  menos al mismo tiempo, el peor caso es un ciclo de identificación extra de
  espera — el clip nunca se corta.

---

## 7. Desinstalación

```sh
systemctl disable --now svxlink-txqueue.service
rm /etc/systemd/system/svxlink-txqueue.service
rm -r /etc/systemd/system/svxlink-txqueue.service.d      # si se creó
systemctl daemon-reload
rm /usr/local/bin/svxlink-txqueue
# elimina 'COMMAND_PTY=...' de [SimplexLogic] en /etc/svxlink/svxlink.conf, luego:
systemctl restart svxlink
rm -r /var/spool/svxlink-tx                              # opcional: elimina el spool
```

---

## Apéndice A — `/usr/local/bin/svxlink-txqueue`

```python
#!/usr/bin/python3
"""
svxlink-txqueue - transmit pre-recorded WAV files over SvxLink.

Drop a *.wav file into  /var/spool/svxlink-tx/incoming/  and this service will:

  1. wait until the file stops growing, then move it to  queue/
  2. process the queue strictly one file at a time, oldest first
  3. convert it to SvxLink's internal format (16 kHz / 16-bit / mono WAV)
     and stage it under  staging/  with a space-free name
  4. wait until the RF channel is idle (no open squelch, transmitter off)
  5. tell the SimplexLogic to play it:
        echo 'EVENT ::playFile <staged-wav>' > $COMMAND_PTY
     SvxLink keys Tx1, plays the clip through its message handler, unkeys
  6. move the original file to  sent/  (or  failed/ on error, with a .log sidecar)

Audio is converted in pure Python (no ffmpeg/sox): any PCM WAV (8/16/24/32-bit,
mono or stereo, any sample rate) is accepted, down-mixed to mono and resampled.

Matching SvxLink config: /etc/svxlink/svxlink.conf -> [SimplexLogic] COMMAND_PTY.
"""

from __future__ import annotations

import argparse
import array
import os
import re
import signal
import sys
import time
import traceback
import wave
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration (override via environment)
# ---------------------------------------------------------------------------
SPOOL        = Path(os.environ.get("TXQUEUE_SPOOL", "/var/spool/svxlink-tx"))
INCOMING     = SPOOL / "incoming"
QUEUE        = SPOOL / "queue"
STAGING      = SPOOL / "staging"
SENT         = SPOOL / "sent"
FAILED       = SPOOL / "failed"

COMMAND_PTY  = os.environ.get("TXQUEUE_COMMAND_PTY", "/dev/shm/svxlink_simplex_ctrl")
SVXLINK_LOG  = os.environ.get("TXQUEUE_SVXLINK_LOG", "/var/log/svxlink")
TARGET_RATE  = int(os.environ.get("TXQUEUE_TARGET_RATE", "16000"))   # SvxLink internal rate

STABLE_S        = float(os.environ.get("TXQUEUE_STABLE_SECONDS", "2.0"))  # incoming file quiet this long
IDLE_GUARD_S    = float(os.environ.get("TXQUEUE_IDLE_GUARD", "3.0"))      # channel idle this long
IDLE_MAX_S      = float(os.environ.get("TXQUEUE_IDLE_MAX_WAIT", "600"))   # ... but wait at most this long
TX_ON_TIMEOUT_S = float(os.environ.get("TXQUEUE_TX_ON_TIMEOUT", "20"))    # wait for TX to key after trigger
TX_OFF_MARGIN_S = float(os.environ.get("TXQUEUE_TX_OFF_MARGIN", "15"))    # slack on top of clip duration
SVX_TX_TIMEOUT_S = float(os.environ.get("TXQUEUE_SVX_TX_TIMEOUT", "295")) # < [SimplexLogic] TIMEOUT=300
POLL_S       = 0.5

_stop = False


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _handle_signal(signum, _frame):
    global _stop
    _stop = True
    log(f"received signal {signum}, shutting down after current file")


# ---------------------------------------------------------------------------
# WAV loading / conversion
# ---------------------------------------------------------------------------
def _frames_to_int16_mono(raw: bytes, width: int, channels: int) -> array.array:
    """Decode interleaved PCM frames -> mono signed-16 samples (average of channels)."""
    if width == 2:
        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder == "big":
            samples.byteswap()
    elif width == 1:
        # WAV 8-bit is unsigned, midpoint 128
        samples = array.array("h", ((b - 128) << 8 for b in raw))
    elif width == 3:
        n = len(raw) // 3
        samples = array.array("h", [0]) * n
        for i in range(n):
            b0, b1, b2 = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
            v = b0 | (b1 << 8) | (b2 << 16)
            if v & 0x800000:
                v -= 0x1000000
            samples[i] = v >> 8
    elif width == 4:
        ints = array.array("i")
        ints.frombytes(raw)
        if sys.byteorder == "big":
            ints.byteswap()
        samples = array.array("h", (v >> 16 for v in ints))
    else:
        raise ValueError(f"unsupported sample width: {width} bytes")

    if channels <= 1:
        return samples

    # down-mix to mono
    mono = array.array("h", [0]) * (len(samples) // channels)
    for i in range(len(mono)):
        acc = 0
        base = i * channels
        for c in range(channels):
            acc += samples[base + c]
        mono[i] = max(-32768, min(32767, acc // channels))
    return mono


def _resample_linear(mono: array.array, src_rate: int, dst_rate: int) -> array.array:
    if src_rate == dst_rate or len(mono) == 0:
        return mono
    n_src = len(mono)
    n_dst = max(1, int(n_src * dst_rate / src_rate))
    out = array.array("h", [0]) * n_dst
    step = src_rate / dst_rate
    pos = 0.0
    last = n_src - 1
    for i in range(n_dst):
        j = int(pos)
        if j >= last:
            out[i] = mono[last]
        else:
            frac = pos - j
            out[i] = int(mono[j] * (1.0 - frac) + mono[j + 1] * frac)
        pos += step
    return out


_safe_re = re.compile(r"[^A-Za-z0-9._-]+")


def stage_wav(src: Path) -> tuple[Path, float]:
    """Convert src to a 16 kHz / 16-bit / mono WAV under STAGING/. Return (path, seconds).

    The staged name is space-free ASCII because the COMMAND_PTY 'EVENT' parser
    splits on whitespace and does not honour quoting.
    """
    with wave.open(str(src), "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        nframes = w.getnframes()
        raw = w.readframes(nframes)

    mono = _frames_to_int16_mono(raw, width, channels)
    mono = _resample_linear(mono, rate, TARGET_RATE)
    if sys.byteorder == "big":
        mono.byteswap()
    duration = len(mono) / float(TARGET_RATE)

    base = (_safe_re.sub("_", src.stem)[:80]).strip("._") or "clip"
    staged = STAGING / f"{base}.{os.getpid()}.{int(time.time())}.wav"
    with wave.open(str(staged), "wb") as o:
        o.setnchannels(1)
        o.setsampwidth(2)
        o.setframerate(TARGET_RATE)
        o.writeframes(mono.tobytes())
    os.chmod(staged, 0o644)
    return staged, duration


# ---------------------------------------------------------------------------
# Channel-idle tracking (tail the SvxLink log)
# ---------------------------------------------------------------------------
class ChannelState:
    _sql_re = re.compile(r"(\S+): The squelch is (OPEN|CLOSED)")
    _tx_re = re.compile(r"Turning the transmitter (ON|OFF)")

    def __init__(self, logpath: str):
        self.logpath = logpath
        self._fh = None
        self._ino = None
        self.open_rx: set[str] = set()
        self.tx_on = False
        self.last_busy = 0.0

    def _ensure_open(self):
        try:
            st = os.stat(self.logpath)
        except FileNotFoundError:
            if self._fh:
                self._fh.close()
                self._fh = None
            return
        if self._fh is None or st.st_ino != self._ino:
            if self._fh:
                self._fh.close()
            self._fh = open(self.logpath, "r", errors="replace")
            self._ino = st.st_ino
            self._fh.seek(0, os.SEEK_END)

    def poll(self):
        self._ensure_open()
        if not self._fh:
            return
        while True:
            line = self._fh.readline()
            if not line:
                break
            m = self._sql_re.search(line)
            if m:
                name, stt = m.group(1), m.group(2)
                if name == "Voter":
                    continue  # aggregate line, not a real receiver
                if stt == "OPEN":
                    self.open_rx.add(name)
                else:
                    self.open_rx.discard(name)
            m = self._tx_re.search(line)
            if m:
                self.tx_on = (m.group(1) == "ON")
        if self.open_rx or self.tx_on:
            self.last_busy = time.monotonic()

    @property
    def busy(self) -> bool:
        return bool(self.open_rx) or self.tx_on


# ---------------------------------------------------------------------------
# Command PTY
# ---------------------------------------------------------------------------
def _svxlink_ready() -> bool:
    return os.path.islink(COMMAND_PTY) or os.path.exists(COMMAND_PTY)


def _pty_write(text: str) -> None:
    fd = os.open(COMMAND_PTY, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
    try:
        os.write(fd, text.encode("ascii", "strict"))
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Queue handling
# ---------------------------------------------------------------------------
def ingest_incoming(seen: dict):
    now = time.monotonic()
    for entry in sorted(INCOMING.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        try:
            st = entry.stat()
        except FileNotFoundError:
            continue
        key = entry.name
        prev = seen.get(key)
        if prev is None or prev[0] != st.st_size or prev[1] != st.st_mtime:
            seen[key] = (st.st_size, st.st_mtime, now)
            continue
        if now - prev[2] < STABLE_S:
            continue
        # stable -> move to queue
        seen.pop(key, None)
        if not entry.name.lower().endswith(".wav") or st.st_size < 44:
            dest = FAILED / entry.name
            _move(entry, dest)
            _write_sidecar(dest, "not a usable .wav file (bad extension or too small)")
            log(f"rejected {entry.name} -> failed/")
            continue
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = _safe_re.sub("_", entry.name)
        dest = QUEUE / f"{stamp}__{safe}"
        _move(entry, dest)
        log(f"queued {entry.name} -> {dest.name}")


def _move(src: Path, dst: Path):
    try:
        src.rename(dst)
    except OSError:
        import shutil
        shutil.move(str(src), str(dst))


def _write_sidecar(dst: Path, text: str):
    try:
        dst.with_suffix(dst.suffix + ".log").write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n"
        )
    except OSError:
        pass


def next_queued() -> Path | None:
    files = sorted(p for p in QUEUE.iterdir() if p.is_file() and not p.name.startswith("."))
    return files[0] if files else None


def wait_for_idle(chan: ChannelState) -> bool:
    """Block until the channel has been idle for IDLE_GUARD_S. Returns False if asked to stop."""
    deadline = time.monotonic() + IDLE_MAX_S
    warned = False
    while not _stop:
        chan.poll()
        svx_ok = _svxlink_ready()
        if svx_ok and not chan.busy:
            idle_for = time.monotonic() - max(chan.last_busy, 0.0)
            if chan.last_busy == 0.0 or idle_for >= IDLE_GUARD_S:
                return True
        if time.monotonic() > deadline:
            log("idle wait timed out - transmitting anyway")
            return True
        if not svx_ok and not warned:
            log(f"waiting: svxlink not ready ({COMMAND_PTY} missing)")
            warned = True
        time.sleep(POLL_S)
    return False


def transmit(path: Path, chan: ChannelState) -> bool:
    log(f"preparing {path.name}")
    if not _svxlink_ready():
        raise RuntimeError(f"{COMMAND_PTY} missing - svxlink not running")

    staged, duration = stage_wav(path)
    try:
        if duration > SVX_TX_TIMEOUT_S:
            raise RuntimeError(
                f"clip is {duration:.0f}s, over the {SVX_TX_TIMEOUT_S:.0f}s limit "
                f"(SimplexLogic TIMEOUT=300); refusing to transmit"
            )
        if not wait_for_idle(chan):
            return False  # stopping; leave original in queue/

        chan.poll()
        keyed = chan.tx_on  # tolerate TX already up (e.g. an ident in progress)
        log(f"transmitting {path.name} ({duration:.1f}s) via {COMMAND_PTY}")
        try:
            _pty_write(f"EVENT ::playFile {staged}\n")
        except OSError as e:
            raise RuntimeError(f"write to {COMMAND_PTY} failed: {e}") from e

        # phase 1: wait for the transmitter to key
        t0 = time.monotonic()
        while not keyed and (time.monotonic() - t0) < TX_ON_TIMEOUT_S:
            if _stop:
                return False
            chan.poll()
            if chan.tx_on:
                keyed = True
                break
            time.sleep(POLL_S)
        if not keyed:
            raise RuntimeError(
                f"transmitter never keyed within {TX_ON_TIMEOUT_S:.0f}s of "
                f"'EVENT ::playFile' (bad file? logic busy? check /var/log/svxlink)"
            )

        # phase 2: wait for a stable transmitter-off, bounded by clip duration
        hard_deadline = time.monotonic() + duration + TX_OFF_MARGIN_S
        while time.monotonic() < hard_deadline:
            if _stop:
                break
            chan.poll()
            if not chan.tx_on:
                time.sleep(1.0)
                chan.poll()
                if not chan.tx_on:
                    break
            time.sleep(POLL_S)
        else:
            log(f"WARNING: {path.name} - transmitter still on at deadline; proceeding")

        log(f"done {path.name}")
        return True
    finally:
        try:
            staged.unlink()
        except OSError:
            pass


def process_queue(chan: ChannelState):
    while not _stop:
        path = next_queued()
        if path is None:
            return
        try:
            ok = transmit(path, chan)
            if not ok:
                return  # stop requested; leave file in queue
            _move(path, SENT / path.name)
        except Exception:
            tb = traceback.format_exc()
            log(f"ERROR transmitting {path.name}:\n{tb}")
            dest = FAILED / path.name
            _move(path, dest)
            _write_sidecar(dest, tb)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--oneshot", action="store_true",
                    help="process whatever is queued/incoming right now, then exit")
    args = ap.parse_args()

    for d in (INCOMING, QUEUE, STAGING, SENT, FAILED):
        d.mkdir(parents=True, exist_ok=True)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    chan = ChannelState(SVXLINK_LOG)
    seen: dict = {}

    log(f"svxlink-txqueue started (spool={SPOOL}, command_pty={COMMAND_PTY}, "
        f"rate={TARGET_RATE})")

    if args.oneshot:
        for _ in range(int(STABLE_S / POLL_S) + 2):
            ingest_incoming(seen)
            time.sleep(POLL_S)
        process_queue(chan)
        log("oneshot complete")
        return

    while not _stop:
        chan.poll()
        ingest_incoming(seen)
        if next_queued() is not None:
            process_queue(chan)
        time.sleep(POLL_S)

    log("stopped")


if __name__ == "__main__":
    main()
```

---

## Apéndice B — `/etc/systemd/system/svxlink-txqueue.service`

```ini
[Unit]
Description=SvxLink WAV transmit queue (inject pre-recorded audio on the air)
Documentation=file:/usr/local/bin/svxlink-txqueue
After=svxlink.service
Wants=svxlink.service
BindsTo=svxlink.service

[Service]
Type=simple
User=svxlink
Group=svxlink
ExecStart=/usr/bin/python3 /usr/local/bin/svxlink-txqueue
Restart=on-failure
RestartSec=5

# hardening
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/var/spool/svxlink-tx /dev/shm
ReadOnlyPaths=/var/log/svxlink
RestrictAddressFamilies=AF_UNIX
IPAddressDeny=any

[Install]
WantedBy=multi-user.target
```

Si tu SvxLink se ejecuta como un usuario distinto de `svxlink`, cambia
`User=`/`Group=` y la propiedad del directorio de spool para que coincidan.

---

## Apéndice C — portar a una lógica con otro nombre

Todo depende del nombre de la sección de lógica. Si tu lógica es `[MyRepeater]`:

- `svxlink.conf`: pon `COMMAND_PTY=/dev/shm/myrepeater_ctrl` en `[MyRepeater]`.
- servicio: `Environment=TXQUEUE_COMMAND_PTY=/dev/shm/myrepeater_ctrl`.
- La llamada `EVENT ::playFile` usa el espacio de nombres TCL **raíz** (`::`),
  así que es independiente del nombre de la lógica — no hacen falta ediciones
  de TCL.
- La lectura del log para detectar canal libre coincide con
  `Turning the transmitter (ON|OFF)` y `<Rx>: The squelch is (OPEN|CLOSED)`, que
  toda lógica emite — también independiente del nombre.
