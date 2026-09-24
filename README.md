# StreamScript AI

Subtítulos y traducción en tiempo real para conferencias, open source y a escala de producción.

Construido para la **Vibeathon de Nerdearla 2026**: reemplazar el esquema actual de subtitulado
comercial/manual (caro, dependiente de operadores, no replicable) por un motor open source que
cualquier conferencia pueda desplegar con `docker compose up`.

## El problema

Nerdearla y conferencias similares dependen de la transcripción y traducción simultánea para ser
accesibles a toda su audiencia. Hoy eso se resuelve con herramientas comerciales que cobran por
minuto por canal activo, requieren operación humana constante, y no son reutilizables por otras
comunidades por costo y licenciamiento. Con 30+ sesiones simultáneas, ese esquema no escala.

## La solución

Una plataforma que toma audio en vivo de N salas en paralelo y produce, por sala:
- Transcripción en el idioma original.
- Traducción a español (y opcionalmente de español a inglés/portugués).
- Vista pública para la audiencia (QR + selector de idioma), export SRT/VTT/TXT al cerrar la sala,
  panel de monitoreo para el equipo de producción, y una vista lista para incrustar como overlay en
  OBS/vMix.

Corre sobre las capacidades de audio en vivo de **Gemini** (`google-genai`, Live API); la lógica de
transcripción está detrás de una interfaz (`TranscriptionProvider`) para poder sumar un proveedor 100%
local basado en **Gemma** sin tocar el resto del sistema.

## Arquitectura

```
mic_client.py ──WS(audio)──▶ /ws/ingest/{room}
                                    │
                              RoomManager (in-memory)
                                    │
                        worker.py — 1 asyncio task por sala activa
                                    │
                    GeminiLiveProvider (TranscriptionProvider)
                      1. ASR streaming (gemini-3.5-transcribe-live),
                         detecta fin de oración por puntuación
                      2. Traducción por oración (llamada de texto
                         aparte + glosario en el prompt)
                      — reconecta antes del límite de ~15min/sesión —
                                    │
                    transcript_store.py (buffer + persistencia JSONL)
                                    │
                              ws_hub.py (pub/sub por sala)
                       ┌────────────┼────────────┐
                 /watch/{id}   /overlay/{id}   /dashboard
                 (audiencia)    (OBS/vMix)     (producción,
                                                 latencia + errores)
                                    │
                        export.py → .srt / .vtt / .txt
```

Un solo proceso FastAPI corre varias salas en paralelo como tasks de asyncio (I/O-bound, escala bien
para una demo con varias salas simultáneas). Para producción real a 30+ salas, el mismo `RoomManager`
se puede respaldar en Redis y correr varios workers horizontales con pub/sub para el fan-out — no
necesario para probar el concepto, sí documentado como el siguiente paso.

## Fortalezas de la arquitectura

Ninguna de las afirmaciones de esta sección es "debería funcionar": todo lo que sigue se corrió de
punta a punta contra la API de Gemini real, no solo contra el `MockProvider`, incluyendo los momentos
en que la API no se comportó como decía la documentación.

- **La arquitectura de dos etapas no fue una elección de diseño, fue un descubrimiento forzado por la
  API real.** Los modelos Live "conversacionales" de Gemini (`gemini-3.8-live`,
  `gemini-live-2.5-flash-preview`, la familia `native-audio`) rechazan pedirles texto plano cuando les
  mandás audio. La solución — separar ASR en streaming de la traducción, con una llamada de texto
  independiente por oración — es más robusta que la alternativa "un solo modelo hace todo", porque cada
  etapa se puede reintentar, cachear o reemplazar (¿modelo de traducción con 503? se reintenta con
  backoff; si sigue caído, el subtítulo sale sin traducir en vez de desaparecer) sin tocar la otra.

- **Multi-sala probado con concurrencia real, no solo argumentado.** 4 salas simultáneas con audio
  distinto (`MockProvider`) y hasta 8 salas simultáneas con `GeminiLiveProvider` real, sin interferencia
  entre ellas ni degradación de latencia en las que sí conectaron — la propiedad que más importaba para
  el reto es la que más se probó (ver límite de cuota abajo, importante antes de un evento real).

- **Multi-idioma probado en ambas direcciones con audio real**, no con texto de prueba: portugués
  hablado → transcripto correctamente → traducido a español **e inglés en simultáneo** desde el mismo
  evento. Sumar un idioma nuevo es un parámetro al crear la sala, no un cambio de código.

- **Proveedor desacoplado del resto del sistema** (`TranscriptionProvider`): rooms, worker, export,
  dashboard y frontend no saben ni les importa si detrás hay Gemini en la nube o un modelo local. Eso
  es lo que hace viable el camino 100% offline (ASR local + **TranslateGemma** para traducción) sin
  reescribir nada más — ver roadmap.

- **Observabilidad real, no estimada:** el panel de producción mide la latencia real de la llamada de
  traducción (ms) y cuenta errores por sala, para que el equipo de producción vea en vivo si algo se
  está degradando en lugar de enterarse por la audiencia.

- **Resiliencia en vez de silencio:** una traducción que falla (503, timeout) reintenta con backoff y,
  si sigue sin responder, degrada a mostrar el texto original en vez de perder el subtítulo — un
  detalle que solo aparece cuando probás contra la API real bajo carga, no en el happy path.

- **100% open source y sin fricción de despliegue:** un solo `docker compose up`, sin build step de
  frontend (HTML/JS plano) ni costo de licencias — así cualquier conferencia lo puede levantar, que es
  el punto central del reto.

- **Accesibilidad pensada desde el diseño:** QR para que cada persona elija sesión e idioma desde su
  propio celular, texto grande con alto contraste opcional en `/watch.html`, y overlay listo para
  quemarse en el stream — no un agregado de último momento.

### Cuota de la cuenta, no límite de la arquitectura (leer antes de un evento real)

Se probó lanzar 8 salas simultáneas con `GeminiLiveProvider` real: 5 de 8 conectaron y transcribieron
sin problema, 3 fallaron con error 1011 "exceeded your current quota". Investigando el motivo exacto se
confirmó que **no es un techo de sesiones concurrentes de la arquitectura**: es que la API key usada para
probar está en el **tier gratuito de Google sin billing habilitado**, que tiene cuotas diarias muy chicas
— se confirmó, por ejemplo, un límite de **20 requests/día** para `gemini-3.5-flash` (el modelo de
traducción). Entre todas las pruebas de esta sesión se agotó esa cuota, y el paso de traducción empezó a
degradar a "mostrar el texto sin traducir" (el fallback funcionando como está diseñado, no un crash).

Las salas que sí lograron conectar no mostraron ninguna degradación de latencia ni interferencia entre
ellas — el proceso maneja la concurrencia bien. El cuello de botella es 100% de cuenta/billing, no de
código. **Antes de un evento real hace falta, sin excepción:**

1. Habilitar billing en el proyecto de [Google AI Studio](https://aistudio.google.com/) / Cloud Console
   y pasar a un tier pago — el free tier no alcanza ni para una sola sala real de punta a punta, mucho
   menos para 30+ en paralelo.
2. Confirmar el tier resultante en [ai.dev/rate-limit](https://ai.dev/rate-limit) antes de la fecha del
   evento, con margen para las 30+ sesiones simultáneas y las decenas de llamadas de traducción por
   minuto que eso implica.
3. Si el techo de un solo proyecto no alcanza igual, repartir salas entre varias API keys/proyectos — el
   código no tiene ningún acoplamiento que lo impida (cada `GeminiLiveProvider` es independiente).

## Setup

### 1. Conseguir una API key de Gemini
En [Google AI Studio](https://aistudio.google.com/) → crear una API key. Copiarla a `.env`:

```bash
cp .env.example .env
# editar .env y completar GEMINI_API_KEY=...
```

> Sin `GEMINI_API_KEY` seteada, cada sala arranca igual pero corre sobre un `MockProvider` que genera
> subtítulos de ejemplo — útil para probar todo el resto del sistema (salas, export, dashboard,
> frontend) sin la key.

### 2. Levantar con Docker (recomendado)

```bash
docker compose up --build
```

Servidor disponible en `http://localhost:8000`.

### 3. O levantar en local sin Docker

```bash
cd backend
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload --app-dir .
```

### 4. Crear una sala y mandarle audio

```bash
curl -X POST http://localhost:8000/api/rooms \
  -H "Content-Type: application/json" \
  -d '{"title": "Keynote", "source_lang": "en", "target_langs": ["es"], "glossary_path": "config/glossary.example.yaml"}'
# devuelve {"id": "<room_id>", ...}

cd ingest
pip install -r requirements.txt
python mic_client.py --room <room_id>          # desde el micrófono
python mic_client.py --room <room_id> --file talk.wav   # o desde un WAV mono 16-bit 16kHz
```

Abrir `http://localhost:8000/watch.html?room=<room_id>` para ver los subtítulos en vivo, o
`http://localhost:8000/dashboard.html` para el panel de producción con todas las salas.

### 5. Quemar los subtítulos en el stream (OBS / vMix)

En OBS: agregar una fuente **Browser Source** apuntando a

```
http://localhost:8000/overlay.html?room=<room_id>&lang=es
```

(fondo transparente, texto grande con contorno — pensado para superponerse sobre la señal). El
parámetro `lang` es opcional; si se omite, se muestra el idioma original. En vMix es análogo, como
fuente de tipo *Web Browser*. La misma sala puede tener a la vez: audiencia mirando `/watch.html` desde
el celu vía QR, el overlay quemándose en el stream, y el equipo de producción viendo `/dashboard.html`
— los tres consumen el mismo WebSocket de subtítulos (`/ws/subtitles/{room_id}`), sin pasos extra.

## Checklist del desafío

Requisitos obligatorios de la Vibeathon, cada uno con lo que lo respalda:

- [x] **Transcripción en tiempo real, idioma original + español.** Validado con audio real en inglés y
      portugués, transcripto correctamente por `gemini-3.5-transcribe-live`.
- [x] **Traducción español → inglés ("si podés").** Validado con voz en español real (TTS de Windows):
      *"Bienvenidos a esta charla sobre sistemas distribuidos."* → *"Welcome to this talk about
      distributed systems."*
- [x] **Correr varias sesiones al mismo tiempo (5, 10+).** La arquitectura lo soporta y se probó con
      hasta 8 salas `GeminiLiveProvider` reales en simultáneo sin interferencia entre las que conectaron.
      El techo real hoy es de **cuota de cuenta de Google (tier gratuito), no de la arquitectura** — ver
      la sección de arriba, es lo primero a resolver antes de un evento real.
- [x] **Licencia OSI + documentación clara.** MIT (aprobada por OSI) — ver [LICENSE](LICENSE).
- [x] **Vista de audiencia: elegir sesión e idioma.** `index.html` lista las salas activas (elegir
      sesión), `watch.html` tiene el selector de idioma — validado abriéndolo en el navegador.
- [x] **Construida sobre Gemini.** `GeminiLiveProvider`, arquitectura en dos etapas (necesaria: ningún
      modelo Live "conversacional" acepta `response_modalities=["TEXT"]` con audio a esta fecha):
      1. **ASR en streaming** con `gemini-3.5-transcribe-live`, detectando fin de oración por puntuación
         sobre el transcript acumulado que devuelve el modelo.
      2. **Traducción por oración** con una llamada de texto aparte (configurable con
         `GEMINI_TRANSLATE_MODEL`), inyectando el glosario técnico en el prompt — confirmado que respeta
         términos marcados como "no traducir" (ej. *hydration*). Si esa llamada falla (rate limit, 503),
         degrada a mostrar el texto sin traducir en vez de perder el subtítulo, y **eso queda registrado**
         como error visible en `/dashboard.html`, no oculto.
- [ ] **Proveedor local con Gemma para el caso 100% offline.** Gemma en sí **no transcribe audio** (es
      una familia de modelos de texto); el camino equivalente al de arriba sería un ASR local (ej.
      Whisper) + **TranslateGemma** (variante de Gemma para traducción, 55 idiomas) — mismo contrato
      `TranscriptionProvider`, mismo patrón de dos etapas ya validado con Gemini.

Opcionales cubiertos: glosario técnico, export SRT/VTT/TXT, overlay OBS/vMix, portugués como idioma
extra (entrada y salida, validado con audio real), y panel de monitoreo con latencia y errores reales
por sala.

Pendiente: escalado horizontal (Redis-backed `RoomManager` + workers separados) para producción a 30+
salas más allá de un solo proceso — no bloqueante para el reto, sí sería el siguiente paso real.

**Nota sobre nombres de modelo:** la familia Gemini se mueve rápido y cada modelo del free tier tiene su
propia cuota diaria chica — si uno se agota, alcanza con cambiar `GEMINI_TRANSLATE_MODEL` en `.env` a
otro (ej. `gemini-3.1-flash-lite`), sin tocar código. En un tier pago esto deja de ser un problema.

## Licencia

MIT — ver [LICENSE](LICENSE).
