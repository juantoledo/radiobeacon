# radiobeacon

*English version: [README.md](README.md).*

**RadioBeacon** es el software detrás de una estación experimental de
radioaficionado: transmite de forma automática, en una frecuencia fija.
Los radioaficionados llaman a una estación usada así una *baliza de
propagación*.

En lugar de transmitir solo un identificador de estación, RadioBeacon
también pone al aire boletines breves de interés público: alertas
tempranas de protección civil e informes de sismos. Quien escucha la
baliza obtiene dos cosas a la vez: la confirmación de que el camino de la
señal funciona y el boletín en sí.

---

## Cómo funciona

RadioBeacon vigila un pequeño conjunto de fuentes de información en
internet. Cuando aparece algo nuevo, convierte ese aviso en un anuncio
hablado breve (o en un mensaje de texto digital) y lo emite por radio
cuando la frecuencia está libre. Cada boletín se transmite varias veces,
con un intervalo fijo entre repeticiones, para que quien sintoniza tarde
aún tenga oportunidad de captarlo.

El recorrido de un boletín:

1. **Vigilar novedades** — RadioBeacon consulta cada fuente de información
   según su propio calendario.
2. **Recopilar** — Cada aviso nuevo se guarda, conservando intacta su
   redacción original.
3. **Resumir (opcional)** — Un aviso extenso se condensa en dos o tres
   frases claras. Este paso usa IA, permanece desactivado salvo que el
   operador lo active, y se omite en avisos que ya son breves.
4. **Preparar el mensaje** — El texto se convierte en audio hablado en
   español, o en un paquete digital. Se añaden automáticamente el
   indicativo de la estación y una nota de que se trata de un relé no
   oficial.
5. **Esperar frecuencia libre** — No se transmite nada mientras otra
   estación esté usando el canal.
6. **Al aire** — El boletín se emite y se repite unas cuantas veces con un
   intervalo fijo entre repeticiones.

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

Cada uno de estos pasos queda registrado en la consola del operador, de
modo que el operador puede ver exactamente qué se recibió y qué salió al
aire.

## Cómo se conecta con la radio

RadioBeacon no controla el transmisor directamente. Prepara un archivo de
audio listo para reproducir y lo entrega al software de control del
transmisor de la estación — [SvxLink](https://www.svxlink.org/) — que
activa la radio y reproduce el clip solo cuando el canal está libre.

RadioBeacon es solo de transmisión. Nunca escucha, decodifica ni graba a
otras estaciones: no tiene receptor de ningún tipo.

## Qué sale al aire

El contenido proviene de fuentes que elige el operador. Dos vienen
configuradas de fábrica:

- **Alertas tempranas de SENAPRED** — avisos de protección civil de Chile
  para eventos meteorológicos, hidrológicos y geofísicos.
- **Informes de sismos del CSN** — eventos sísmicos publicados por el
  Centro Sismológico Nacional.

El operador puede añadir más fuentes — cualquier feed web público —
describiéndolas en la consola. No hace falta programar.

RadioBeacon es un relé experimental y no oficial. Toda alerta que
transmite lo indica así, y remite a los oyentes a SENAPRED como fuente
oficial. No es un servicio de radiodifusión de emergencias y no debe
usarse como tal.

## El papel de la IA

La inteligencia artificial cumple una única función acotada en
RadioBeacon: resumir un aviso extenso en unas pocas frases completas.
Trabaja a partir de una instrucción fija que le indica no inventar nada y
no dejar ninguna frase sin terminar. Es independiente del proveedor —
OpenAI, Claude, o un modelo Ollama autoalojado, según la configuración —
y con Ollama es completamente local.

La IA nunca decide qué se transmite, nunca cambia la configuración ni la
identidad de la estación, y está desactivada por defecto. Los avisos que
ya son suficientemente breves nunca se le envían.

Si el paso de IA falla, el operador elige de antemano qué ocurre: usar el
texto original, usar solo el titular, o no transmitir nada.

## Seguridad y control del operador

- **En silencio por defecto.** Una instalación nueva no transmite nada. El
  operador debe activar la transmisión y completar la identidad de la
  estación — indicativo, ubicación, frecuencia — antes de que algo salga
  al aire.
- **Un solo tipo de mensaje a la vez.** La estación envía voz o paquete
  digital, nunca ambos al mismo tiempo.
- **La frecuencia libre es lo primero.** Las transmisiones esperan a que
  el canal esté libre.
- **Nada se acumula.** Un boletín que ha esperado demasiado caduca en
  lugar de saturar el aire más tarde.
- **Registro completo.** Cada descarga, cada resumen y cada transmisión
  queda registrada en la consola, y cualquier clip de voz puede
  reproducirse en el navegador.

## Por dentro

Las fuentes de información son servicios públicos de organismos del
Estado. RadioBeacon las consulta a través de las mismas interfaces web que
usan los sitios de esos organismos: no hay acceso especial ni convenio de
por medio. Cada fuente se consulta con su propio temporizador
independiente, de modo que una fuente rápida y una lenta no se frenan
entre sí.

Una emergencia en desarrollo suele aparecer como una serie de
actualizaciones, no como un único aviso editado. RadioBeacon trata cada
actualización como un boletín propio y nunca las reescribe ni las
combina. Todo lo que ya vio se ignora, así el mismo reporte no sale al
aire dos veces.

RadioBeacon nunca activa la radio por sí mismo. De eso se encarga
SvxLink, el software consolidado que controla el transmisor de la
estación y comparte la frecuencia de forma cortés con otros operadores.
RadioBeacon arma un clip de audio terminado y lo deja en una cola;
SvxLink reproduce los clips de a uno, y solo después de que la frecuencia
haya estado en silencio unos segundos. Un clip que esperó demasiado se
descarta en vez de salir tarde.

En modo digital, un boletín sale como un paquete de datos corto en lugar
de voz. RadioBeacon usa Direwolf, un programa de paquete de radioafición
muy difundido, para convertir el texto en los tonos que decodifica un
receptor de paquete. Esos paquetes usan una frecuencia experimental y
nunca tocan la red pública APRS.

Salvo la descarga de actualizaciones de las fuentes —y el paso opcional
de resumen— todo funciona en la máquina del operador sin conexión a
internet: la voz, los tonos de paquete, la cola y el panel.

## Qué no es RadioBeacon

- No es un canal oficial de emergencias, ni un sustituto de SENAPRED,
  ONEMI ni de ningún sistema estatal de alertas.
- No es un sistema bidireccional ni de despacho: no puede recibir
  mensajes ni confirmarlos.
- No es un producto comercial. Es una única estación experimental,
  operada por un radioaficionado con licencia y compartida como código
  abierto.

---

## Inicio rápido

```bash
./start.sh   # clon nuevo: crea .env, prepara un venv compartido y arranca
             # junto la recolección de datos + la entrega + las acciones +
             # el panel + la baliza (Ctrl+C detiene todo). No requiere
             # secretos; la baliza arranca deshabilitada
             # (BEACON_ENABLED=false).
```

Con todo en marcha, abre `http://127.0.0.1:8080` — el panel del operador.

## Más información

- [documentation/ARCHITECTURE.md](documentation/ARCHITECTURE.md) — cómo
  encajan los siete componentes, las políticas de transmisión, y cómo
  ejecutar cada uno por separado. *(en inglés)*
- [documentation/CONFIGURATION.md](documentation/CONFIGURATION.md) — la
  referencia de `.env`, la identidad de la estación, el logging y la
  importación/exportación de configuración. *(en inglés)*
- [documentation/svxlink-txqueue-SETUP.es.md](documentation/svxlink-txqueue-SETUP.es.md)
  — cómo configurar la entrega al aire hacia SvxLink.
- Cada componente tiene su propio README (en inglés):
  [data-adapters/](data-adapters/README.md),
  [dispatcher/](dispatcher/README.md), [mq/](mq/README.md),
  [actions/](actions/README.md), [ui/](ui/README.md),
  [beacon/](beacon/README.md), [storage/](storage/README.md).
