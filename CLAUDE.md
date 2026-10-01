# Servicio de Resúmenes — Contexto del proyecto

Capacidad vertical para la cátedra de Microservicios (Unidad 4 en adelante). Se construye sobre el chat base del curso (`users`, `channels`, `messages`), que corre en `localhost:8001/8002/8003` y expone su material en https://github.com/rmasad/microservicios.

## Problema que resuelve

En chats grupales con alto volumen de mensajes, los usuarios pierden contexto cuando no revisan el chat por un tiempo. El servicio genera resúmenes automáticos vía IA de dos formas:

- Bajo demanda, acotado a un `timeInterval`.
- Automáticamente al entrar al chat, si hay más de `messageThreshold` mensajes nuevos desde el último resumen.

El resumen prioriza decisiones tomadas, eventos y asignaciones de tareas.

## Lenguaje ubicuo

| Término | Significado |
| --- | --- |
| `summary` | Resumen generado para un canal, con su contenido y metadatos |
| `timeInterval` | Rango horario solicitado para acotar los mensajes a resumir |
| `messageThreshold` | N° límite de mensajes nuevos que gatilla un resumen automático |
| `messageState` | Estado de una solicitud: `pendiente`, `procesando`, `completada`, `fallida` |

## Frontera de responsabilidad

- Este servicio es dueño exclusivo de `summary`, `digest` y `checkpoint`, en su propia base `summaries-mongodb`.
- **No** accede a las bases de datos de `users`, `channels` ni `messages`. Toda interacción con el núcleo pasa por sus APIs públicas o por los eventos que ellos publican en `/api/v1/events`.
- Puede usar IDs de usuarios, canales y mensajes para relacionar un resumen con su contexto, pero no es propietario de esas entidades.

## Decisión de arquitectura: asíncrono con polling

`POST /api/v1/channels/{channel_id}/summaries` **no bloquea** esperando al LLM. Responde `202 Accepted` + `summary_id` de inmediato. El cliente hace polling a `GET /api/v1/summaries/{summary_id}` hasta que `messageState` sea `completada` o `fallida`.

## APIs expuestas

| Método | Ruta | Función |
| --- | --- | --- |
| POST | `/api/v1/channels/{channel_id}/summaries` | Solicitar un resumen (202 + `summary_id`) |
| GET | `/api/v1/summaries/{summary_id}` | Estado/contenido de un resumen |
| GET | `/api/v1/channels/{channel_id}/summaries` | Listar resúmenes de un canal |
| GET | `/api/v1/channels/{channel_id}/summaries/latest` | Último resumen disponible |

## APIs consumidas del núcleo

| Método | Ruta | Función |
| --- | --- | --- |
| GET | `/api/v1/channels/{channel_id}` | Validar que el canal exista |
| GET | `/api/v1/users/{user_id}` | Validar que el usuario exista |
| GET | `/api/v1/channels/{channel_id}/messages` | Completar/reconstruir la ventana de mensajes |
| POST | `<LLM_API>/generate` | Enviar mensajes + prompt, recibir el resumen |

## Eventos

| Evento | Dirección | Función |
| --- | --- | --- |
| `chat.message.created` | Consumido (continuo, en paralelo) | Mantener al día el historial local, independiente de cualquier solicitud |
| `summary.requested` | Consumido | Dispara la generación de un resumen |
| `summary.generated` | Publicado | El resumen se generó correctamente |
| `summary.failed` | Publicado | La generación del resumen falló |

## Flujo end2end (10 pasos, el 3 corre en paralelo y continuo)

1. Cliente: `POST /api/v1/channels/{channel_id}/summaries` → Gateway responde `202` + `summary_id`.
2. Gateway publica `summary.requested` en el broker.
3. *(paralelo, continuo)* `users`/`channels`/`messages` publican `chat.message.created`; el servicio mantiene su historial local al día.
4. El servicio consume `summary.requested`.
5. Construye la ventana de mensajes y llama a `POST <LLM_API>/generate`.
6. El LLM devuelve el resumen.
7. El servicio guarda `summary` y actualiza `messageState` en `summaries-mongodb`.
8. Publica `summary.generated` (o `summary.failed`).
9. El broker notifica al Gateway.
10. El cliente hace polling con `GET /api/v1/summaries/{summary_id}` hasta recibir el resumen.

## Stack sugerido / restricciones de la tarea

- FastAPI para la API REST.
- Debe correr con Docker/docker-compose, en la red `microsvcs` del chat base.
- Documentación OpenAPI, versionamiento y manejo de errores explícitos.
- Emitir un evento por cada transacción.
- Repositorio público para la entrega.

## Documento fuente

Propuesta completa (con diagramas de arquitectura y flujo end2end): https://claude.ai/artifact/BSeEEVB7yRPouL1wqsg497

## Hallazgos del código base del curso (`microservicios/03_Comunicación/demo`)

Verificado leyendo el código; tener en cuenta antes de implementar.

### Broker y eventos
- **El broker es RabbitMQ** (patrón visto en `04_Implementación/demo_04`, con `pika`; imagen `rabbitmq:4-management-alpine`, puertos 5672 y UI 15672, red externa `microsvcs`).
- **El compose del demo 03 NO levanta RabbitMQ.** Hay que agregarlo (o levantar el de `demo_04/message_broker`) y el host será el nombre del servicio en la red `microsvcs`.
- `messages` solo publica al broker si existe la env `RABBITMQ_URL` (en el compose actual **no está definida**, así que hoy no publica nada). Hay que setearla (p. ej. `amqp://rabbitmq:5672`) para recibir `chat.message.created`.
- `messages` publica en el exchange **`chat`**, tipo `topic`, declarado **no durable** (`exchange_declare(exchange="chat", exchange_type="topic")`). Si el servicio declara el exchange con `durable=True` RabbitMQ fallará con `PRECONDITION_FAILED`: declararlo con los mismos parámetros (no durable) o usar `passive=True`. Los mensajes se publican sin `delivery_mode` persistente.
- `routing_key` = tipo del evento (`chat.message.created`). `users` y `channels` **no publican al broker**, solo guardan en una lista en memoria (`GET /api/v1/events`); sus eventos son `chat.user.created` y `chat.channel.created`.
- Si el broker está caído, `messages` ignora el error silenciosamente (sin outbox): puede haber mensajes que nunca lleguen como evento → por eso se reconstruye con `GET .../messages`.
- Formato del evento (JSON):
  ```json
  {"type": "chat.message.created", "subject_id": "<id>", "occurred_at": "<ISO8601 UTC>", "data": {...}}
  ```
  Usar el mismo formato (`type`, `subject_id`, `occurred_at`, `data`) para `summary.*`. Los eventos del servicio deben publicarse en su propio exchange (p. ej. `summaries`, topic, durable) y exponer también `GET /api/v1/events` por consistencia con el curso.
- Patrón de consumo del demo_04: cola durable propia por servicio (`<servicio>_for_<x>_queue`), `queue_bind` con routing key, `basic_ack` manual. Usar una cola propia para `chat.message.created` (si no, se compite con otros consumidores).
- `pika.BlockingConnection` es bloqueante: en FastAPI correrlo en un hilo/proceso aparte (o usar `aio-pika` en el `lifespan`).

### Contratos del núcleo
- Puertos locales: users 8001, channels 8002, messages 8003; **dentro de la red `microsvcs` todos escuchan en 8000** → `http://users:8000`, `http://channels:8000`, `http://messages:8000`.
- IDs son `uuid4` en string (campo `id`, no `_id`).
- `GET /api/v1/users/{id}` → `{id, display_name}`; 404 `{"detail": "User not found"}`.
- `GET /api/v1/channels/{id}` → `{id, name}`; 404 `{"detail": "Channel not found"}`.
- Mensaje: `{id, channel_id, author_id, content, created_at}` (`created_at` ISO8601 UTC, `content` hasta 4000 caracteres). Sin campo de hilo/estado.
- `GET /api/v1/channels/{id}/messages` devuelve **lista completa sin paginación, sin filtro por fecha y sin garantía de orden** (orden de inserción de Mongo): filtrar por `created_at` y ordenar en el servicio. Ojo con canales grandes.
- Los servicios responden con errores `{"detail": "..."}` (FastAPI estándar); `messages` usa 404 canal inexistente, 422 autor inexistente, 502/503 dependencias caídas, y timeout de 2 s hacia otros servicios (usar un timeout similar).
- No hay autenticación en ningún servicio ni Gateway en el demo 03 (el Gateway del curso aparece en las unidades 4–5, GraphQL, `demo_04/api-gateway`). Por tanto, `summary.requested` probablemente lo debe publicar el propio servicio.
- Eventos de usuarios/canales no llevan el nombre del autor en el mensaje: para el prompt del LLM resolver `author_id` → `display_name` vía `GET /users/{id}` (con caché).

### Convenciones del stack del curso
- Python 3.12-slim, `fastapi==0.115.12`, `uvicorn[standard]==0.34.2`, `pymongo==4.12.1`, `pika==1.3.2`; Mongo `mongo:7` con volumen propio. Dockerfile: `COPY app ./app` + `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- Base de datos del servicio con su propio contenedor (`summaries-mongodb`) y volumen (`summaries-data`).
- Red: `networks: microsvcs: name: microsvcs` (en compose propio usar `external: true`; el demo 03 la crea, hay que levantarlo primero).
- Todas las rutas bajo `/api/v1`, más `GET /health` → `{"status": "ok"}`.
- Cada transacción emite evento; el patrón del demo es una función `publish(event_type, subject_id, data)` que guarda en la lista local `events` y publica al broker.

### Pendiente por definir (no está en el repo del curso)
- URL, autenticación y formato de `<LLM_API>/generate` (no existe en el código revisado).
- Si habrá un Gateway real que publique `summary.requested`; mientras tanto, publicarlo y consumirlo desde este mismo servicio.

## Estado de implementación

### Esqueleto (paso 1 completado)
Levanta con `docker compose up -d --build` (requiere el chat base corriendo: crea la red `microsvcs`, que aquí es `external: true`). Servicio en `localhost:8004` (`/docs`, `/health`). Verificado: `/health`, OpenAPI con los 4 endpoints, conexión a Mongo y RabbitMQ.

Estructura actual:
- `app/main.py` — FastAPI 1.0.0, `lifespan` (abre `MongoClient`; ahí se lanzarán los consumidores), `/health`, `GET /api/v1/events`.
- `app/config.py` — variables de entorno (`MONGODB_URL`, `BROKER_URL`, `USERS_URL`, `CHANNELS_URL`, `MESSAGES_URL`, `LLM_API_URL`, `LLM_API_KEY`, `LLM_TIMEOUT_SECONDS`, `MESSAGE_THRESHOLD`, `CORE_TIMEOUT_SECONDS`) y nombres de exchanges (`chat` para consumir, `summaries` para publicar).
- `app/models.py` — enum `MessageState`, `TimeInterval`, `SummaryRequest`, `Summary`, `SummaryAccepted`, `Checkpoint`.
- `app/errors.py` — `ServiceError` y subclases (`ChannelNotFound` 404, `UserNotFound` 422, `SummaryNotFound` 404, `DependencyFailed` 502, `DependencyUnavailable` 503), respuesta `{"detail": ...}`.
- `app/routes/summaries.py` — los 4 endpoints; **aún devuelven 501**. `latest` se declara antes que la ruta con parámetro.
- `app/events.py` — `publish()` solo guarda en la lista local `events` (falta publicar en RabbitMQ).
- `app/clients/core_client.py`, `llm_client.py` — stubs (`NotImplementedError`). Un solo cliente del núcleo, no uno por servicio.

### Decisiones tomadas
- **RabbitMQ propio** en el compose (`summaries-rabbitmq`, `rabbitmq:4-management-alpine`) sin publicar puertos al host, porque 5672/15672 suelen estar ocupados por otros proyectos locales. Ojo: el `messages` del núcleo solo publica si se le define `RABBITMQ_URL` apuntando a un broker que este servicio pueda alcanzar; hay que usar el **mismo broker** (decidir si el núcleo apunta a `summaries-rabbitmq` o este servicio al del núcleo).
- **`pymongo` síncrono** (como el curso), sin `motor`; `httpx` para clientes HTTP; `pika` para el broker.
- **Variables de entorno:** `docker-compose.yaml` carga `.env.example` (`env_file`) y sobrescribe `MONGODB_URL` y `BROKER_URL`. `LLM_API_URL` apunta a un `llm-mock` inexistente hasta que se defina el LLM real.
- Puerto host `8004` → contenedor `8000`.

### Siguiente
Paso 2: modelos persistentes y repositorio Mongo (`summaries`, `digests`, `checkpoints`, historial local de mensajes).
