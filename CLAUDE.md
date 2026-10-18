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
8. Publica `summary.generated` (o `summary.failed`) con entrega fiable (paso 8).
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

### Completado (pasos 1–6)
Servicio en `localhost:8004`: persistencia, cliente HTTP, ventanas, REST/polling y RabbitMQ. POST devuelve 202 y la solicitud llega a la bandeja durable del worker. Permanece pendiente hasta implementar el LLM del paso 7.

- `app/main.py`: lifespan de Mongo/HTTP y del hilo RabbitMQ, health y events.
- `app/models.py`, `app/repository.py`: summaries, digests, checkpoints e historial local.
- `app/clients/core_client.py`: get_channel, get_user y list_messages con validación y errores explícitos. `llm_client.py` sigue como stub.
- `app/services.py`: solicitudes y ventanas reconciliadas, con snapshot inmutable y cobertura de tardíos.
- `app/events.py`: publicador/consumidores, confirmaciones, reconexión y cola de errores.
- `tests/`: 159 pruebas aprobadas con Mongo/RabbitMQ reales. Ejecución en README; conceptos y contratos en [docs/REFERENCIA.md](./docs/REFERENCIA.md).

### Decisiones tomadas
- **RabbitMQ propio** en el compose (`summaries-rabbitmq`, `rabbitmq:4-management-alpine`) sin publicar puertos al host, porque 5672/15672 suelen estar ocupados por otros proyectos locales. Ojo: el `messages` del núcleo solo publica si se le define `RABBITMQ_URL` apuntando a un broker que este servicio pueda alcanzar; hay que usar el **mismo broker** (decidir si el núcleo apunta a `summaries-rabbitmq` o este servicio al del núcleo).
- **`pymongo` síncrono** (como el curso), sin `motor`; `httpx` para clientes HTTP; `pika` para el broker.
- **Variables de entorno:** `docker-compose.yaml` carga `.env.example` (`env_file`) y sobrescribe `MONGODB_URL` y `BROKER_URL`. `LLM_API_URL` apunta a un `llm-mock` inexistente hasta que se defina el LLM real.
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

### Siguiente
Paso 7: worker sobre la bandeja recibida, cliente LLM HTTP y mock de pruebas. Después: 8 eventos de resultado fiables; 9 Gateway/entrada automática; 10 end2end y entrega pública. Funcionamiento esperado y criterios detallados en [README.md](./README.md).

Las cuatro decisiones consultadas quedaron resueltas por el usuario. Restan datos de despliegue del LLM real y la implementación/conexión del Gateway.
