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
                    GeminiLiveProvider (system instruction + glosario,
                    reconecta antes del límite de ~15min por sesión)
                                    │
                    transcript_store.py (buffer + persistencia JSONL)
                                    │
                              ws_hub.py (pub/sub por sala)
                       ┌────────────┼────────────┐
                 /watch/{id}   /overlay/{id}   /dashboard
                 (audiencia)    (OBS/vMix)     (producción)
                                    │
                        export.py → .srt / .vtt / .txt
```

Un solo proceso FastAPI corre varias salas en paralelo como tasks de asyncio (I/O-bound, escala bien
para una demo con varias salas simultáneas). Para producción real a 30+ salas, el mismo `RoomManager`
se puede respaldar en Redis y correr varios workers horizontales con pub/sub para el fan-out — no
necesario para probar el concepto, sí documentado como el siguiente paso.

## Por qué esta solución

- **100% open source**, pensada para desplegarse con infraestructura como código (Docker) sin costo de
  licencias.
- **Flexible**: Gemini en la nube para máxima precisión, o un proveedor local (Gemma) sin dependencia
  de internet externo — mismo contrato, sin reescribir el resto del sistema.
- **Sin build step de frontend**: HTML/JS plano servido por el propio backend, para que cualquier
  conferencia lo levante con un solo comando.

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

## Estado actual / roadmap

- [x] Scaffold: FastAPI + estructura de proyecto + Docker.
- [x] Pipeline core: ingest de audio → proveedor de transcripción → subtítulos en vivo.
- [x] Multi-sala: N salas concurrentes en un solo proceso — validado con 4 salas simultáneas (`MockProvider`)
      y con 2 salas simultáneas corriendo `GeminiLiveProvider` real, sin interferencia entre ellas.
- [x] Vista de audiencia (QR + selector de idioma), export SRT/VTT/TXT, glosario técnico, dashboard,
      overlay para OBS/vMix.
- [x] `GeminiLiveProvider` validado contra la API real. Arquitectura en dos etapas (necesaria: ningún
      modelo Live "conversacional" acepta `response_modalities=["TEXT"]` con audio a esta fecha):
      1. **ASR en streaming** con `gemini-3.5-transcribe-live` (Live API), detectando fin de oración por
         puntuación sobre el transcript acumulado que devuelve el modelo.
      2. **Traducción por oración** con una llamada de texto aparte (`gemini-3.5-flash` por defecto,
         configurable con `GEMINI_TRANSLATE_MODEL`), inyectando el glosario técnico en el prompt — se
         confirmó que respeta términos marcados como "no traducir" (ej. *hydration*).
- [ ] Proveedor local con Gemma para el caso 100% offline. Gemma en sí **no transcribe audio** (es una
      familia de modelos de texto); el camino equivalente al de arriba sería un ASR local (ej. Whisper)
      + **TranslateGemma** (variante de Gemma para traducción, 55 idiomas) para el paso de traducción —
      mismo contrato `TranscriptionProvider`, mismo patrón de dos etapas ya validado con Gemini.
- [ ] Escalado horizontal real (Redis-backed `RoomManager` + workers separados) para 30+ salas en
      producción.

**Nota sobre nombres de modelo:** la familia Gemini se mueve rápido — `gemini-3.8-flash` es la
recomendación oficial actual para texto pero devolvía 503 por demanda alta al momento de probar; se
dejó `gemini-3.5-flash` como default por ser el que respondió de forma estable. Si eso cambia, alcanza
con setear `GEMINI_TRANSLATE_MODEL` en `.env`, sin tocar código.

## Licencia

MIT — ver [LICENSE](LICENSE).
