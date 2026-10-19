# Guion para el video de demostración

Duración sugerida: **4–6 minutos**. Se muestra el código y se prueba la API desde Swagger (la interfaz de FastAPI), con el **chat simulado** como fuente de conversaciones y **Gemini real** generando los resúmenes.

## Antes de grabar

1. Tener el archivo `.env` con `LLM_API_KEY=...` (no se muestra en pantalla: no abras ese archivo durante la grabación).
2. Levantar la demo (esperar a que termine; tarda ~1 min la primera vez):

   ```bash
   docker network create microsvcs          # una sola vez, si no existe
   docker compose -f docker-compose.yaml -f docker-compose.demo.yaml up -d --build --wait
   docker compose -f docker-compose.yaml -f docker-compose.demo.yaml ps
   ```

3. Abrir dos pestañas: **resúmenes** `http://localhost:8004/docs` y **chat simulado** `http://localhost:8010/docs`.
4. Tener el editor abierto en los archivos del punto 1 del guion, y una terminal visible.

> Los datos del chat simulado viven en memoria. Si algo sale mal y quieres empezar de cero:
> `docker compose -f docker-compose.yaml -f docker-compose.demo.yaml restart chat-sim` (vuelve a la conversación de ejemplo).

## Guion

### 1. El código (≈1,5 min)

Recorre en este orden, diciendo en una frase qué hace cada uno:

| Archivo | Qué decir |
| --- | --- |
| `app/routes/summaries.py` | Los 4 endpoints REST bajo `/api/v1`. El `POST` responde **202** y no espera al LLM. |
| `app/models.py` | `Summary` y `StructuredSummary`: el LLM devuelve decisiones, eventos y tareas como campos; el texto lo arma el código. |
| `app/clients/llm_client.py` | Cliente de Gemini con **salida estructurada** (`RESPONSE_SCHEMA`), modelo de respaldo y manejo de cuota. |
| `app/worker.py` | El worker que genera el resumen, con **lease** para recuperarse de caídas. |
| `app/events.py` y `docs/eventos/` | Los eventos que se emiten por cada transacción y sus esquemas JSON. |
| `docker-compose.yaml` | Servicio + Mongo propio + RabbitMQ en la red `microsvcs`. |

### 2. El chat simulado (≈0:30)

En `http://localhost:8010/docs`, ejecuta **`GET /api/v1/channels`** y **`GET /api/v1/users`**. Copia el `id` del canal `proyecto-final` y el de **Ana**. Opcional: **`GET /api/v1/channels/{id}/messages`** para mostrar la conversación (12 mensajes con decisiones, eventos y tareas).

### 3. Pedir un resumen (≈1,5 min) — en `http://localhost:8004/docs`

1. **`POST /api/v1/channels/{channel_id}/summaries`** con el id del canal y este cuerpo:

   ```json
   { "user_id": "ID-DE-ANA" }
   ```

   Mostrar: **202**, el `summary_id` y el encabezado `location`. Decir que la respuesta es inmediata (asíncrono con polling).
2. **`GET /api/v1/summaries/{summary_id}`**: ejecutarlo una o dos veces hasta ver `messageState: "completada"` (tarda 2–3 s). Mostrar `content` (decisiones, eventos y tareas con su responsable) y `structured` (los mismos datos como campos).
3. **`GET /api/v1/channels/{channel_id}/summaries`** (listado) y **`.../summaries/latest`** (el último completado).

### 4. Resumen por intervalo (opcional, ≈0:30)

`POST` con un rango (ajusta las fechas a las últimas horas, con zona horaria):

```json
{ "timeInterval": { "start": "2026-10-19T00:00:00Z", "end": "2026-10-20T00:00:00Z" } }
```

### 5. Resumen automático (≈1 min)

1. En el chat simulado, **`POST /api/v1/channels/{id}/messages`** unas 6 veces con `author_id` de Ana (por ejemplo: «Revisemos el informe», «Carla agrega la sección faltante», «Entregamos mañana»...). Hay que superar el umbral de la demo (5 mensajes nuevos).
2. **`POST /api/v1/channels/{id}/enter`** con `{ "user_id": "ID-DE-ANA" }` (Gateway simulado: publica `chat.channel.entered`).
3. De vuelta en resúmenes: **`GET /api/v1/channels/{channel_id}/summaries`**. Aparece un resumen nuevo que nadie pidió. Si repites el `enter` sin mensajes nuevos, no se crea otro.

### 6. Errores (≈0:30)

- `POST .../channels/no-existe/summaries` → **404** `{"detail": "Channel not found"}`.
- `GET .../summaries?limit=0` → **422** con el detalle de la validación.
- `POST` con `{"user_id": "nadie"}` → **422** `User does not exist`.

### 7. Eventos y pruebas (≈0:45)

- **`GET /api/v1/events`**: se ven `summary.requested` y `summary.generated` de cada resumen. Abrir `docs/eventos/summary.generated.schema.json` para mostrar el esquema.
- En la terminal, mostrar que corren las pruebas (≈40 s; se puede grabar un fragmento o mostrar el resultado):

  ```bash
  docker compose -p summaries-tests -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests --attach tests
  docker compose -p summaries-tests -f compose.test.yaml down -v
  ```

## Si algo falla durante la grabación

| Síntoma | Qué hacer |
| --- | --- |
| El resumen queda `fallida` con «cuota … agotada» | Es el límite gratuito de Gemini: espera un minuto y pide otro resumen. Si el modelo principal se agotó para el día, cambia `LLM_MODEL` en `.env` y `docker compose ... up -d`. |
| El servicio no arranca y habla de `LLM_API_KEY` | Falta el `.env` con la key. |
| El resumen automático no aparece | Revisa que hayas publicado más de 5 mensajes nuevos **después** del último resumen incremental. |
| `POST` devuelve 503 | El chat simulado o Mongo no están listos: `docker compose ... ps`. |

## Al terminar

```bash
docker compose -f docker-compose.yaml -f docker-compose.demo.yaml down -v
docker network rm microsvcs        # solo si la creaste para la demo y nada más la usa
```
