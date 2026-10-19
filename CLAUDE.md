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
| `digest` | Resultado inmutable del LLM para una solicitud, con contenido y trazabilidad de su ventana efectiva; relación 1:1 con summary |
| `checkpoint` | Cursor por canal del último resumen incremental confirmado; no es un cursor de lectura por usuario |
| `LocalMessage` | Proyección local del contrato público de messages, reconstruible desde sus APIs/eventos |
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
| `summary.requested` | Publicado y consumido | Registra el trabajo para el worker de generación |
| `summary.generated` | Publicado | El resumen se generó correctamente |
| `summary.failed` | Publicado | La generación del resumen falló |

## Flujo end2end (10 pasos, el 3 corre en paralelo y continuo)

1. Cliente: `POST /api/v1/channels/{channel_id}/summaries` → el servicio valida referencias, guarda solicitud/evento y responde `202` + `summary_id`.
2. El publicador del servicio envía `summary.requested` al broker.
3. *(paralelo, continuo)* `messages` publica `chat.message.created`; el servicio mantiene su historial local al día.
4. El servicio consume `summary.requested` y registra su recepción durable; el worker del paso 7 tomará ese trabajo.
5. Construye/reutiliza la ventana de mensajes y llama a `POST <LLM_API>/generate` (paso 7).
6. El LLM devuelve el resumen.
7. El servicio guarda digest/summary y confirma la cobertura incremental en `summaries-mongodb`.
8. Publica `summary.generated` (o `summary.failed`) con entrega fiable.
9. El Gateway consume los resultados y publicará la entrada al canal para el disparo automático (paso 9).
10. El cliente hace polling con `GET /api/v1/summaries/{summary_id}` hasta recibir el resumen.

## Stack sugerido / restricciones de la tarea

- FastAPI para la API REST.
- Debe correr con Docker/docker-compose, en la red `microsvcs` del chat base.
- Documentación OpenAPI, versionamiento y manejo de errores explícitos.
- Emitir un evento por cada transacción.
- Repositorio público para la entrega.

## Documento fuente

Propuesta completa (con diagramas de arquitectura y flujo end2end): https://claude.ai/artifact/BSeEEVB7yRPouL1wqsg497

## Estado de implementación

### Progreso por paso
| Paso | Descripción | Estado |
| --- | --- | --- |
| 1 | Esqueleto FastAPI, Dockerfile, compose (Mongo + RabbitMQ), `/health` | Hecho |
| 2 | Modelos y repositorio Mongo (summaries, digests, checkpoints, messages) | Hecho |
| 3 | Cliente del núcleo con errores explícitos | Hecho |
| 4 | `POST /channels/{id}/summaries` (202 + Location) y ventanas de mensajes | Hecho |
| 5 | Los 3 `GET` restantes (por id, listado paginado, latest) | Hecho |
| 6 | RabbitMQ: consumidor de `chat.message.created`, bandeja durable de `summary.requested` | Hecho |
| 7 | Worker + cliente LLM (mock y Gemini) | Hecho |
| 8 | Publicar `summary.generated` / `summary.failed` con entrega fiable | Hecho |
| 9 | Resumen automático por entrada al canal (Gateway) y `messageThreshold` | Pendiente |
| 10 | End2end, pulido, README y entrega pública | Pendiente |

El POST devuelve 202 y la solicitud llega a la bandeja durable del worker; permanece `pendiente` hasta implementar el paso 7. Servicio en `localhost:8004` (`/docs`, `/health`); requiere el chat base corriendo (crea la red `microsvcs`, aquí `external: true`).

Archivos:
- `app/main.py`: lifespan de Mongo/HTTP y del hilo RabbitMQ, health y events.
- `app/models.py`, `app/repository.py`: summaries, digests, checkpoints e historial local.
- `app/clients/core_client.py`: get_channel, get_user y list_messages con validación y errores explícitos. `llm_client.py`: `LLMProvider`/`LLMRequest`, `MockProvider`, `GeminiProvider` y `build_provider`.
- `app/worker.py`: worker de generación (lease con dueño, intentos, recuperación de cobertura, límites de ventana).
- `app/services.py`: solicitudes y ventanas reconciliadas, con snapshot inmutable y cobertura de tardíos.
- `app/events.py`: publicador/consumidores, confirmaciones, reconexión y cola de errores.
- `tests/`: pruebas con Mongo/RabbitMQ reales (`compose.test.yaml`, `Dockerfile.test`; ejecución en README e `INSTRUCCIONES.md`, que está en PowerShell). Conceptos y contratos en [docs/REFERENCIA.md](./docs/REFERENCIA.md), que también contiene los hallazgos del código base del curso.
- El trabajo de los pasos 2–6 entró por la rama `avance-paso-2-3-4-5-6` (PR #1 a `main`). Repo: https://github.com/marcel-silva27/summaries-service

### Decisiones tomadas
- **RabbitMQ propio** en el compose (`summaries-rabbitmq`, `rabbitmq:4-management-alpine`) sin publicar puertos al host, porque 5672/15672 suelen estar ocupados por otros proyectos locales. Ojo: el `messages` del núcleo solo publica si se le define `RABBITMQ_URL` apuntando a un broker que este servicio pueda alcanzar; hay que usar el **mismo broker** (decidir si el núcleo apunta a `summaries-rabbitmq` o este servicio al del núcleo).
- **`pymongo` síncrono** (como el curso), sin `motor`; `httpx` para clientes HTTP; `pika` para el broker.
- **Variables de entorno y secretos:** `docker-compose.yaml` carga `.env.example` (valores por defecto versionados, sin secretos) y luego `.env` (opcional, ignorado por git) que lo sobrescribe. Las credenciales reales (`LLM_API_KEY`) van **solo** en `.env`, nunca en `.env.example`. Compose fija `MONGODB_URL`; `BROKER_URL` admite override. `LLM_API_URL` apunta a un `llm-mock` inexistente hasta el paso 7.
- Puerto host `8004` → contenedor `8000`.

### Decisiones tomadas (paso 2)
- **Persistencia:** base summaries; colecciones summaries, digests, checkpoints y messages; volumen propio y sin TTL.
- **Digest:** resultado inmutable, uno por summary; IDs públicos string y claves únicas en Mongo.
- **Estados:** transiciones condicionales; un solo claim y estados terminales idempotentes.
- **Consultas:** UTC en milisegundos, ventanas [start,end), orden por fecha/ID; latest considera solo completados.
- **Checkpoint:** por canal, avance explícito solo tras resumen incremental completado; messages_since calculado al leer.
- **Atomicidad:** por documento; digest → summary → checkpoint. Eventos y recuperación en la orquestación.

### Decisiones tomadas (paso 3)
- **HTTP:** un CoreClient síncrono compartido en el lifespan, con pool, timeout configurable y cierre al apagar.
- **Contratos:** respuestas validadas, sin filtrar ni truncar historial; canal validado por get_channel, no por una lista vacía.
- **Errores:** 404 de usuario → 422; 404 de canal → 404; indisponibilidad → 503; respuesta inválida → 502. Sin reintentos automáticos ni redirecciones.

### Decisiones tomadas (paso 4)
- **Ventanas:** reconciliar por API antes del primer armado; guardar IDs/intervalo como snapshot por solicitud y reutilizarlo en reintentos.
- **Incrementales:** seleccionar mensajes sin cobertura, incluidos los tardíos; marcar solo los IDs confirmados. No modificar resúmenes publicados.
- **Cursor:** mantenerlo monotónico; messages_since cuenta cobertura pendiente, incluso antes del cursor. Esta decisión actualiza la semántica temporal del paso 2.

### Decisiones tomadas (paso 5)
- **REST:** validar canal/usuario, guardar solicitud y evento en una escritura y responder 202 con Location; polling por ID usa solo Mongo.
- **Consultas:** listado paginado y latest completado; ambos validan el canal por API.

### Decisiones tomadas (paso 6)
- **Broker compartido:** summaries-rabbitmq para ambos servicios; messages conectado mediante RABBITMQ_URL, sin editar el repositorio del curso.
- **Mensajería:** pika en un hilo propio; chat no durable, summaries durable, colas propias y ack manual tras persistir. Eventos inválidos van a una cola de errores.
- **Entrega:** outbox/inbox de solicitudes dentro de summary, publicación confirmada y mensajes persistentes; RabbitMQ con volumen propio y hostname estable.

### Decisiones confirmadas para los pasos siguientes
- **LLM:** API HTTP configurable; mock solo para pruebas. URL/credencial reales se configurarán en el paso 7.
- **Entrada al canal:** evento publicado por un Gateway, sin nueva ruta de entrada en este servicio. Contrato propuesto para implementar en el paso 9 en REFERENCIA.md.

## Revisión de código (2026-10-19) — pendientes a considerar

Resueltos:
- Los secretos ya no pasan por un archivo versionado (ver decisión de variables de entorno).
- **Upsert en lote:** `MongoRepository.upsert_messages` (`bulk_write` no ordenado, lotes de 1000, misma semántica idempotente/de conflicto que `upsert_message`, que ahora delega en él). `build_window` reconcilia con una escritura por lote en vez de 2–3 consultas por mensaje.
- **Outbox por aviso:** `EventBroker.notify()` (un `threading.Event`) despierta la publicación; `SummaryService.request_summary` lo invoca al crear la solicitud (inyectado desde `app.state.broker`). Sin aviso, solo hay un sondeo de respaldo cada `OUTBOX_POLL_SECONDS` (default 5 s) y una publicación al (re)conectar; ya no se consulta Mongo cada 0,2 s. El worker del paso 7 debe reutilizar este patrón (aviso + respaldo).
- `compose.test.yaml`: `mongodb` con `ulimits nofile 64000`; con el límite de 1024 de algunos hosts Linux, WiredTiger abortaba (EMFILE) a mitad de la suite. Suite: 210 pruebas aprobadas.

Abiertos, por prioridad:
- **Operación:** `messages` del núcleo solo publica con `RABBITMQ_URL=amqp://summaries-rabbitmq:5672` aplicado por override en ejecución (repetir si se recrea el contenedor). El paso 9 depende de un Gateway que aún no existe.
- **Docker:** la imagen corre como root y con `--reload`, y el volumen `./app` es de desarrollo; separar configuración de entrega.
- **Documentación:** hay tres guías de ejecución (README, `INSTRUCCIONES.md`, `docs/REFERENCIA.md`); dejar una como autoridad. Verificar el conteo de pruebas citado en el README.

## Decisiones para el paso 7 (LLM: Gemini)

- **Proveedor:** Gemini mediante API key. La suscripción Gemini Plus (app de consumo) no incluye API key: se obtiene en Google AI Studio (tiene capa gratuita con límites de uso, y en esa capa Google puede usar los datos enviados para mejorar sus modelos; considerarlo al enviar mensajes del chat). Verificar nombre de modelo vigente y límites antes de fijarlos.
- **Adaptador:** la API de Gemini no sigue el contrato `POST /generate` propuesto en REFERENCIA.md; `llm_client.py` debe adaptar mensajes + prompt al formato de Gemini (`generateContent`, key por cabecera) y devolver texto. `LLM_API_KEY` solo en `.env`.
- **Límite del MVP:** ventana máxima de **7 días o 500 mensajes** (lo que ocurra primero). Con el contexto de Gemini no hace falta dividir en partes para el MVP (500 × 4000 caracteres máx. es el peor caso). Hay que definir y documentar qué pasa al exceder el límite (propuesta: tomar los 500 más recientes y registrar en el digest que hubo recorte; no truncar en silencio) y cómo interactúa con el checkpoint (los mensajes no resumidos no deben marcarse como cubiertos).

### Implementado del paso 7 (worker + mock)
- **Worker** con lease con dueño: reclamo atómico (pendiente o procesando vencido), renovación antes de cada paso, intentos contados (`WORKER_MAX_ATTEMPTS`=3), reutilización del digest persistido sin volver a llamar al LLM, y confirmación de cobertura recuperable (`_coverage_pending`). Despertado por aviso desde el consumidor de `summary.requested` y por sondeo de respaldo.
- **Ventana vacía** → `fallida` con motivo, sin llamar al LLM. **Errores transitorios** → reintento hasta el máximo y luego `fallida` con "(tras N intentos)".
- **Límites 7 días / 500 mensajes / 400000 caracteres** desde los más recientes. Desviación respecto a lo conversado: los mensajes omitidos **sí se marcan como cubiertos** en resúmenes incrementales (con la idea original, el umbral del paso 9 se dispararía indefinidamente); el recorte queda en `Digest.omitted_count`, en el snapshot y en una nota al final del resumen. Los resúmenes por `timeInterval` no afectan la cobertura.
- `LLM_PROVIDER=mock` es el default (incluido en `.env.example`): con el compose normal las solicitudes se completan con un resumen **simulado**; con `LLM_PROVIDER=gemini` en `.env` se usa Gemini (sin proveedor de pago).
- Pruebas: `tests/conftest.py` desactiva los límites de ventana por defecto (fechas fijas); `tests/test_worker.py` los restablece.

### Gemini (paso 7, completado)
- `GeminiProvider` verificado con la API real (una llamada): modelo principal `gemini-3.5-flash-lite`, respaldo `gemini-3.8-flash` (IDs confirmados con `GET /v1beta/models`). La key vive en `.env` local (ignorado por git; **nunca en `.env.example`**, que está versionado). Para usarlo: `.env` con `LLM_PROVIDER=gemini`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_FALLBACK_MODEL`.
- Límites de cuota gratuita informados por el equipo: Flash-Lite 15 RPM / 250K TPM / 500 RPD; Flash 5 RPM / 250K TPM / 20 RPD. Por eso Flash-Lite es el principal. Las pruebas automáticas nunca llaman a la API real.
- El prompt prohíbe convertir días relativos en fechas (una prueba inicial lo hizo) y pide responder sin introducción.
- Las llamadas de prueba con datos reales envían los mensajes a Google; en la capa gratuita pueden usarse para mejorar sus modelos: usar datos ficticios.

### Prueba de extremo a extremo (2026-10-19) — paso 7 verificado
Chat base (`docker compose -p demo`, override `RABBITMQ_URL`) + summaries con Gemini real (`gemini-3.5-flash-lite`). Resultados:
- POST → 202 con `Location`; polling `pendiente → procesando → completada` en ~2 s; contenido con decisiones, eventos y tareas con responsables correctos; `latest` y listado coherentes.
- Canal inexistente → 404; usuario inexistente → 422; intervalo sin mensajes → `fallida` ("No hay mensajes en la ventana solicitada") sin llamar al LLM.
- Incremental: el 2º resumen solo cubrió el mensaje nuevo (digest de 1 mensaje); el de `timeInterval` no tocó el checkpoint. Al final: 0 mensajes sin cobertura, 0 `_coverage_pending`, sin errores en logs.
- El evento `chat.message.created` (publicado a mano en el exchange `chat`) llegó al historial local.

**Bug del demo del curso (no de este servicio):** en `users`, `channels` y `messages`, `insert_one(dict)` agrega `_id` (ObjectId) al mismo dict que luego se devuelve/publica. Resultado: todos los `POST` de creación responden **500 aunque el dato queda guardado**, y `messages` falla en `json.dumps(event)` **antes de publicar**, así que con el demo tal cual **nunca se emite `chat.message.created`** (el ObjectId no es serializable). Existe igual en el repo remoto (`main`). Consecuencias: (1) para probar hay que tolerar el 500 y obtener los IDs desde los GET/listados (los usuarios no tienen listado: se leyeron de su Mongo solo para preparar la prueba); (2) la reconciliación por `GET .../messages` de `build_window` es imprescindible, no un respaldo menor; (3) para ejercitar el camino por eventos hay que publicar `chat.message.created` a mano en el exchange `chat` (no durable). No se modificaron archivos del curso; conviene avisar al profesor.
- Al bajar el chat con `docker compose -p demo down` desaparece la red `microsvcs` (la crea ese proyecto); los volúmenes se conservan.

### Pendiente tras el paso 7
- Un resumen incremental cuyos mensajes pendientes sean todos más antiguos que 7 días termina `fallida` y no avanza el checkpoint; revisar en el paso 9 para evitar reintentos repetidos por el umbral.

### Paso 8 — eventos terminales (hecho)
- **Evento atómico con el estado:** `_transition` escribe `_terminal_delivery` (evento, `published`, `attempts`, `last_error`) en la misma operación que pasa a `completada`/`fallida`; un worker con lease vencido no lo escribe y re-confirmar no lo duplica.
- **Contrato:** `summary.generated` / `summary.failed` en el exchange `summaries`; `data` = `summary_id`, `channel_id`, `requested_by`, `messageState` (+ `error` en failed). Sin contenido del resumen (se consulta por GET). Detalle en `docs/REFERENCIA.md`.
- **Publicación:** `EventBroker._flush_outbox` publica solicitudes y resultados (persistente, confirmada, `message_id` estable); el worker avisa con `on_result` → `broker.notify`. Los resultados van **sin `mandatory`** porque aún no hay consumidor; quien se suscriba declara su cola durable enlazada a `summary.generated`/`summary.failed`.
- **Eventos durables:** se eliminaron `publish()` y la lista en memoria; `GET /api/v1/events?limit&offset` lee de Mongo (`MongoRepository.list_events`). `make_event` vive en `models.py` (se reexporta desde `events.py`).
- **Diagnóstico:** cada intento de publicación cuenta en `attempts`; un fallo guarda `last_error`.

### Preparado para acoplar un Gateway (sin implementarlo)
El servicio ya cumple su lado del contrato; al Gateway le corresponde:
1. **Recibir resultados:** declarar una cola durable propia enlazada al exchange `summaries` con `summary.generated`/`summary.failed`, deduplicar por tipo + `summary_id` y consultar el contenido por `GET /api/v1/summaries/{id}`. Para ponerse al día tras estar caído: `GET /api/v1/events`.
2. **Avisar la entrada a un canal (paso 9):** publicar `chat.channel.entered` (`channel_id`, `user_id`, `entry_id`) en el exchange `gateway` (propuesta en REFERENCIA.md). Mientras no exista, el paso 9 se probará con un publicador de prueba, no con una ruta nueva en este servicio.
