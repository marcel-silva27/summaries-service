# Servicio de Resúmenes

Capacidad vertical para la cátedra de Microservicios. Genera resúmenes automáticos de las conversaciones del chat base del curso ([rmasad/microservicios](https://github.com/rmasad/microservicios)) con un LLM, priorizando **decisiones tomadas, eventos y asignación de tareas**. No accede a las bases del chat: solo usa sus APIs públicas y los eventos del broker.

Un resumen se genera de dos formas:

- **Bajo demanda**, opcionalmente acotado a un `timeInterval`.
- **Automáticamente** cuando un usuario entra a un canal con más de `MESSAGE_THRESHOLD` mensajes nuevos desde el último resumen.

## Cómo funciona

El `POST` **no espera al LLM**: responde `202` + `summary_id` y el cliente consulta por polling hasta `completada` o `fallida`.

```text
Cliente ──POST /channels/{id}/summaries──▶ API ──(1 escritura en Mongo: solicitud + evento)──▶ 202 + summary_id
                                            │
                      outbox ──▶ RabbitMQ: summary.requested ──▶ inbox ──▶ Worker ──▶ LLM (Gemini)
                                                                              │
Cliente ◀──GET /summaries/{id} (polling)── Mongo ◀── digest + estado + checkpoint ┘
                                                                              └──▶ summary.generated | summary.failed
chat.message.created (continuo) ──▶ historial local        chat.channel.entered (Gateway) ──▶ resumen automático
```

Piezas: API REST (FastAPI), worker de generación con lease y reintentos, hilo de RabbitMQ (consumo e outbox), MongoDB propio (`summaries`, `digests`, `checkpoints`, `messages`, `entries`). Detalle técnico en [`docs/REFERENCIA.md`](./docs/REFERENCIA.md); contexto y decisiones en [`CLAUDE.md`](./CLAUDE.md); qué se verificó, en [`docs/VERIFICACION.md`](./docs/VERIFICACION.md).

## Estado y alcance

El servicio está completo y probado: API REST con polling, eventos por cada transacción, worker con recuperación de fallas, resumen automático, esquemas de eventos y documentación OpenAPI.

Conviene saber antes de usarlo:

- **Necesita una API key de Gemini** para generar resúmenes (ver [Requisitos](#requisitos)); sin ella el servicio no arranca. El proveedor `mock` es solo para pruebas automáticas.
- **No hay Gateway.** El curso no lo entrega. El servicio ya consume `chat.channel.entered` y publica `summary.generated`/`summary.failed`; mientras no exista, la entrada se prueba con una herramienta (ver [Resumen automático](#resumen-automático)).
- **El demo del curso tiene un bug**: sus `POST` de creación responden 500 aunque guardan el dato, y `messages` **nunca publica `chat.message.created`** (falla al serializar el `_id` de Mongo). Por eso el servicio reconcilia el historial con `GET /channels/{id}/messages` en cada ventana. Detalle en [`docs/REFERENCIA.md`](./docs/REFERENCIA.md).
- No hay autenticación (el chat base tampoco la tiene).

## Requisitos

- **Docker y Docker Compose.** No hace falta Python en el equipo.
- **Una API key de Gemini (obligatoria).** El servicio genera los resúmenes con Gemini; sin la key **no arranca** y lo dice con un mensaje claro. Se obtiene gratis en [Google AI Studio](https://aistudio.google.com/) (no la incluye la suscripción de la app de Gemini). Ver [Configurar Gemini](#configurar-gemini).
- **Un chat al que resumir:** el chat base del curso, o el chat simulado de la [demo](#demo-con-un-chat-simulado).

## Configurar Gemini

Crea un archivo `.env` en la raíz del repositorio (está en `.gitignore`):

```env
LLM_API_KEY=tu-key-de-google-ai-studio
```

El resto viene preconfigurado en `.env.example`: modelo principal `gemini-3.5-flash-lite` y de respaldo `gemini-3.8-flash` (cambiables con `LLM_MODEL` y `LLM_FALLBACK_MODEL`; los disponibles se listan con `GET https://generativelanguage.googleapis.com/v1beta/models`). Con el principal agotado (429) o con fallos del proveedor se usa el de respaldo; si ambos fallan, el resumen queda `fallida` con el motivo.

- **La key no se sube a GitHub.** `.env` está ignorado por git y por Docker (no entra en la imagen), y `.env.example` solo trae valores por defecto. **Nunca escribas la key en `.env.example`**, que sí está versionado.
- **Privacidad:** en la capa gratuita Google puede usar lo que se le envía para mejorar sus modelos. Usa conversaciones sin datos personales.
- `LLM_PROVIDER=mock` existe **solo para las pruebas automáticas**: devuelve un resumen simulado y no debe usarse para evaluar el servicio.

### Publicar o desplegar sin exponer la key

La key se entrega al contenedor **en tiempo de ejecución**, nunca dentro del repositorio ni de la imagen:

- **Máquina propia o servidor con Docker Compose:** un `.env` local con permisos restringidos (`chmod 600 .env`).
- **CI/CD (p. ej. GitHub Actions):** guardarla como *secret* del repositorio e inyectarla como variable de entorno `LLM_API_KEY`.
- **Kubernetes:** crear un `Secret` (`kubectl create secret generic gemini --from-literal=LLM_API_KEY=...`) y exponerlo al pod como variable de entorno.
- Si la key se filtra, revócala y crea otra en Google AI Studio; no hace falta tocar el código.

## Cómo correrlo

1. Crear el `.env` con la key (ver arriba).
2. Chat base del curso (en una carpeta aparte, sin modificarlo):

   ```bash
   git clone https://github.com/rmasad/microservicios.git
   docker compose -p demo -f microservicios/03_Comunicación/demo/docker-compose.yaml up -d --build
   ```

3. Este servicio (espera a que Mongo, RabbitMQ y la app estén saludables):

   ```bash
   cd summaries-service
   docker compose up -d --build --wait
   ```

4. Conectar `messages` al broker de este servicio (un override por stdin: no crea ni edita archivos del curso; repetir si se recrea `messages`):

   ```bash
   printf 'services:\n  messages:\n    environment:\n      RABBITMQ_URL: amqp://summaries-rabbitmq:5672\n' \
     | docker compose -p demo -f microservicios/03_Comunicación/demo/docker-compose.yaml -f - up -d --no-deps messages
   ```

5. Listo: API `http://localhost:8004`, OpenAPI `http://localhost:8004/docs`, salud `http://localhost:8004/health`.

Para apagar: `docker compose down` (conserva los volúmenes `summaries-data` y `summaries-rabbitmq-data`; con `-v` se borran). **Desarrollo** con código montado y recarga: `docker compose -f docker-compose.yaml -f docker-compose.dev.yaml up --build`. Guía paso a paso en PowerShell: [`INSTRUCCIONES.md`](./INSTRUCCIONES.md).

## Demo con un chat simulado

El demo del curso responde 500 en sus `POST` de creación y nunca publica `chat.message.created`, lo que dificulta mostrar el servicio. Para eso hay un **chat simulado** (`demo/chat-sim`): responde como `users`, `channels` y `messages` con los mismos contratos pero sin ese bug, trae una conversación de ejemplo (3 usuarios, 12 mensajes con decisiones, eventos y tareas) y hace de Gateway con `POST /api/v1/channels/{id}/enter`. Los datos viven en memoria.

```bash
docker network create microsvcs          # una sola vez, si no existe
docker compose -f docker-compose.yaml -f docker-compose.demo.yaml up -d --build --wait
```

- Servicio de resúmenes: `http://localhost:8004/docs` · Chat simulado: `http://localhost:8010/docs`.
- El umbral automático baja a 5 mensajes en la demo. Guion para mostrarlo desde Swagger: [`docs/DEMO.md`](./docs/DEMO.md).
- Apagar y limpiar: `docker compose -f docker-compose.yaml -f docker-compose.demo.yaml down -v`.

## API

| Método | Ruta | Función |
| --- | --- | --- |
| POST | `/api/v1/channels/{channel_id}/summaries` | Solicitar un resumen → `202` + `summary_id` + `Location` |
| GET | `/api/v1/summaries/{summary_id}` | Estado y contenido (polling; no llama al núcleo) |
| GET | `/api/v1/channels/{channel_id}/summaries?limit&offset` | Listar resúmenes del canal (limit 1–1000) |
| GET | `/api/v1/channels/{channel_id}/summaries/latest` | Último resumen **completado** (404 si no hay) |
| GET | `/api/v1/events?limit&offset` | Eventos durables del servicio, más antiguos primero |
| GET | `/health` | Liveness |

```bash
# Solicitar (cuerpo opcional: user_id y/o timeInterval con zona horaria; el intervalo es [start, end))
curl -i -X POST http://localhost:8004/api/v1/channels/CHANNEL_ID/summaries \
  -H 'Content-Type: application/json' -d '{"user_id":"USER_ID"}'
# Polling hasta messageState = completada | fallida
curl http://localhost:8004/api/v1/summaries/SUMMARY_ID
```

`messageState`: `pendiente` → `procesando` → `completada` | `fallida` (con `error` legible). Un resumen completado trae `content` (texto) y `structured` (los mismos datos en campos: `overview`, `decisions`, `events[{description, when}]`, `tasks[{description, assignee}]`). Errores: `{"detail": "..."}` con 404 (canal/resumen), 422 (usuario inexistente o datos inválidos; los de FastAPI traen una lista en `detail`), 409, 502/503 (núcleo o almacenamiento) y 500 genérico sin detalles internos.

**Ventana enviada al LLM:** como máximo 7 días hacia atrás desde el fin de la ventana, 500 mensajes y 400 000 caracteres, siempre desde los más recientes. Lo omitido se anota al final del resumen y queda registrado en el digest; los mensajes omitidos de un resumen incremental se dan por cubiertos.

### Resumen automático

El Gateway (o cualquier productor) publica `chat.channel.entered` en el exchange durable `gateway` con `channel_id`, `user_id` y `entry_id` (contrato en `docs/REFERENCIA.md`). Si los mensajes sin resumir de los últimos 7 días superan `MESSAGE_THRESHOLD` (50 no dispara; 51 sí), se crea una solicitud normal. Hay a lo sumo una solicitud automática activa por canal y repetir un `entry_id` no abre otra. Para probarlo sin Gateway:

```bash
docker compose exec summaries python -m app.tools.publish_channel_entered CHANNEL_ID USER_ID [ENTRY_ID]
```

## Eventos

| Evento | Dirección | Notas |
| --- | --- | --- |
| `chat.message.created` | Consume (exchange `chat`, no durable) | Mantiene el historial local |
| `summary.requested` | Publica y consume (exchange `summaries`) | Outbox/inbox: solicitud y evento se guardan juntos |
| `summary.generated` / `summary.failed` | Publica (exchange `summaries`) | Se guarda junto al estado final; sin el contenido del resumen |
| `chat.channel.entered` | Consume (exchange `gateway`, durable) | Lo emite el Gateway |

Formato del curso: `{type, subject_id, occurred_at, data}`; la routing key es el `type`. **Esquemas JSON Schema** de cada evento en [`docs/eventos/`](./docs/eventos/), generados desde modelos Pydantic y verificados por las pruebas. Entrega al menos una vez, con identidad estable por evento: un consumidor debe deduplicar por tipo + `summary_id`. Quien consuma los resultados declara su propia cola durable enlazada a `summary.generated`/`summary.failed`.

## Variables de entorno

`docker-compose.yaml` carga `.env.example` (valores por defecto) y luego `.env` (opcional, para secretos y cambios locales).

| Variable | Default | Descripción |
| --- | --- | --- |
| `MONGODB_URL`, `MONGODB_DATABASE`, `MONGODB_TIMEOUT_MS` | contenedor propio, `summaries`, `5000` | Base propia |
| `BROKER_URL` | `amqp://summaries-rabbitmq:5672` | RabbitMQ compartido con `messages` |
| `BROKER_TIMEOUT_SECONDS`, `BROKER_RETRY_SECONDS` | `2`, `1` | Timeout y espera entre reconexiones |
| `OUTBOX_POLL_SECONDS` | `5` | Respaldo del aviso inmediato de la outbox |
| `USERS_URL`, `CHANNELS_URL`, `MESSAGES_URL`, `CORE_TIMEOUT_SECONDS` | `http://{users,channels,messages}:8000`, `2` | Núcleo del chat |
| `LLM_PROVIDER` | `gemini` | `gemini` (el servicio exige la key); `mock` solo para pruebas |
| `LLM_API_KEY` | — | **Obligatoria.** La key de Gemini, **solo en `.env`** |
| `LLM_MODEL`, `LLM_FALLBACK_MODEL` | `gemini-3.5-flash-lite`, `gemini-3.8-flash` | Modelo principal y de respaldo |
| `LLM_API_URL`, `LLM_TIMEOUT_SECONDS`, `LLM_RETRY_MAX_WAIT_SECONDS` | API de Google, `60`, `10` | Endpoint, timeout y espera máxima del reintento ante 429 |
| `LLM_MAX_DAYS`, `LLM_MAX_MESSAGES`, `LLM_MAX_INPUT_CHARS` | `7`, `500`, `400000` | Límites de la ventana |
| `LOG_LEVEL` | `INFO` | Nivel de logs; cada intento a Gemini registra modelo y duración |
| `WORKER_POLL_SECONDS`, `WORKER_RETRY_SECONDS`, `WORKER_LEASE_SECONDS`, `WORKER_MAX_ATTEMPTS` | `5`, `5`, `120`, `3` | Worker; el lease debe superar `LLM_TIMEOUT_SECONDS` |
| `MESSAGE_THRESHOLD` | `50` | El disparo automático es con **más de** este valor |

## Pruebas

Solo requieren Docker (levantan su propio Mongo y RabbitMQ aislados; el núcleo y Gemini se simulan, así que no usan cuota). Desde `summaries-service/`:

```bash
docker compose -p summaries-tests -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests --attach tests
docker compose -p summaries-tests -f compose.test.yaml down -v
```

**266 pruebas aprobadas.** Cubren API y errores, persistencia y concurrencia, ventanas (más de 1000 mensajes, tardíos, límites), outbox/inbox con RabbitMQ real, worker (lease, reintentos, recuperación), adaptador de Gemini y salida estructurada, eventos de resultado y sus esquemas JSON, resumen automático, documentación OpenAPI (válida y con ejemplos) y respuestas públicas.

## Resiliencia verificada con la pila real

| Escenario | Resultado |
| --- | --- |
| RabbitMQ caído al pedir resúmenes | `202`, quedan `pendiente`; al volver el broker se completan solos (~4 s) |
| RabbitMQ recreado (contenedor nuevo, mismo volumen) | La solicitud en vuelo se completa |
| Mongo caído | `POST`/`GET` responden `503`, `/health` sigue ok; al volver, el servicio se recupera sin reiniciar |
| Reinicio del servicio con solicitudes pendientes y broker caído | Al volver el broker se completan todas |
| Servicio matado a mitad de la llamada a Gemini (probado con Gemini real) | El lease vence, otro intento la completa: un solo digest y un solo `summary.generated` |
| Evento duplicado / inválido | Duplicado: un solo efecto. Inválido: cola de errores |

## Estructura

```text
summaries-service/
├── README.md                  # esta guía
├── CLAUDE.md, AGENTS.md       # contexto del proyecto y reglas para agentes
├── INSTRUCCIONES.md           # recorrido paso a paso en PowerShell
├── docs/REFERENCIA.md         # contrato técnico; docs/VERIFICACION.md; docs/eventos/*.schema.json
├── Dockerfile                 # usuario no root, healthcheck, sin --reload
├── docker-compose.yaml        # servicio + Mongo + RabbitMQ (red externa microsvcs)
├── docker-compose.dev.yaml    # override de desarrollo (código montado, --reload)
├── docker-compose.demo.yaml   # demo con el chat simulado (demo/chat-sim); guion del video en docs/DEMO.md
├── compose.test.yaml, Dockerfile.test, requirements*.txt
├── demo/chat-sim/             # chat simulado para la demo
├── tests/
└── app/
    ├── main.py                # lifespan, health, events
    ├── config.py, models.py, errors.py, api_models.py
    ├── event_schemas.py       # esquemas de los eventos del broker
    ├── repository.py          # PyMongo: summaries, digests, checkpoints, mensajes, entradas
    ├── services.py            # solicitudes, ventanas, entradas a canales
    ├── worker.py              # generación con lease, reintentos y recuperación
    ├── events.py              # RabbitMQ: consumo, outbox, topología
    ├── routes/summaries.py    # endpoints REST
    ├── clients/               # core_client (users/channels/messages), llm_client (Gemini; mock para pruebas)
    └── tools/                 # publish_channel_entered, export_event_schemas
```

## Flujo de trabajo

- `main` es la rama estable; el trabajo va en ramas `feature/<nombre>` con PR contra `main`.
- Antes de tocar un contrato con el núcleo, revisar su OpenAPI en `localhost:800{1,2,3}/docs`.
- Si cambia un contrato, el lenguaje ubicuo o la frontera de responsabilidad, actualizar `CLAUDE.md` y `docs/REFERENCIA.md`; si cambia un evento, regenerar `docs/eventos/`.
- El servicio nunca lee ni escribe las bases de `users`, `channels` ni `messages`.
