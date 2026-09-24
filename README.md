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
  distinto (`MockProvider`) y 2 salas simultáneas con `GeminiLiveProvider` real, cada una con su propia
  sesión Live y su propia sesión de traducción, sin interferencia entre ellas ni degradación de latencia
  — la propiedad que más importaba para el reto (5-10+ escenarios en paralelo) es la que más se probó.

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

## Estado actual / roadmap

- [x] Scaffold: FastAPI + estructura de proyecto + Docker.
- [x] Pipeline core: ingest de audio → proveedor de transcripción → subtítulos en vivo.
- [x] Multi-sala: N salas concurrentes en un solo proceso — validado con 4 salas simultáneas (`MockProvider`)
      y con 2 salas simultáneas corriendo `GeminiLiveProvider` real, sin interferencia entre ellas.
- [x] Vista de audiencia (QR + selector de idioma), export SRT/VTT/TXT, glosario técnico, dashboard con
      latencia y conteo de errores por sala, overlay para OBS/vMix (documentado arriba).
- [x] `GeminiLiveProvider` validado contra la API real. Arquitectura en dos etapas (necesaria: ningún
      modelo Live "conversacional" acepta `response_modalities=["TEXT"]` con audio a esta fecha):
      1. **ASR en streaming** con `gemini-3.5-transcribe-live` (Live API), detectando fin de oración por
         puntuación sobre el transcript acumulado que devuelve el modelo.
      2. **Traducción por oración** con una llamada de texto aparte (`gemini-3.5-flash` por defecto,
         configurable con `GEMINI_TRANSLATE_MODEL`), inyectando el glosario técnico en el prompt — se
         confirmó que respeta términos marcados como "no traducir" (ej. *hydration*).
- [x] Más idiomas de entrada y salida: validado con **portugués como idioma de entrada**, traducido en
      simultáneo a español e inglés en el mismo evento (audio real generado con el TTS de Gemini, para
      no depender de una voz PT instalada localmente). Agregar un idioma nuevo es solo config
      (`source_lang`/`target_langs` al crear la sala), no requiere cambios de código.
- [ ] Proveedor local con Gemma para el caso 100% offline. Gemma en sí **no transcribe audio** (es una
      familia de modelos de texto); el camino equivalente al de arriba sería un ASR local (ej. Whisper)
      + **TranslateGemma** (variante de Gemma para traducción, 55 idiomas) para el paso de traducción —
      mismo contrato `TranscriptionProvider`, mismo patrón de dos etapas ya validado con Gemini.
- [ ] Escalado horizontal real (Redis-backed `RoomManager` + workers separados) para 30+ salas en
      producción.

De los opcionales del reto, quedan cubiertos: glosario técnico, export SRT/VTT/TXT, overlay OBS/vMix,
más idiomas (portugués) y panel de monitoreo con latencia/errores por sala.

**Nota sobre nombres de modelo:** la familia Gemini se mueve rápido — `gemini-3.8-flash` es la
recomendación oficial actual para texto pero devolvía 503 por demanda alta al momento de probar; se
dejó `gemini-3.5-flash` como default por ser el que respondió de forma estable. Si eso cambia, alcanza
con setear `GEMINI_TRANSLATE_MODEL` en `.env`, sin tocar código.

## Licencia

MIT — ver [LICENSE](LICENSE).
