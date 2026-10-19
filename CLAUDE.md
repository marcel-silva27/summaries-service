# Servicio de Resúmenes — contexto del proyecto

Microservicio vertical para la cátedra de Microservicios (trabajo final). Se construye sobre el chat base del curso (`users`, `channels`, `messages`; `localhost:8001/8002/8003`, material en https://github.com/rmasad/microservicios). Repositorio público: https://github.com/marcel-silva27/summaries-service.

**Enunciado:** implementar el microservicio asignado como API REST, emitir eventos por cada transacción, con documentación, esquemas, versionamiento y manejo de errores; correr con Docker/docker-compose; código en un repositorio público. *No* exige Gateway, GraphQL ni despliegue en Kubernetes.

Este archivo es el contexto estable del proyecto. Detalle técnico: [`docs/REFERENCIA.md`](./docs/REFERENCIA.md). Cómo ejecutarlo: [`README.md`](./README.md). Qué se verificó: [`docs/VERIFICACION.md`](./docs/VERIFICACION.md). Reglas rápidas para agentes: [`AGENTS.md`](./AGENTS.md).

## Qué hace

En chats grupales con mucho volumen se pierde contexto. El servicio genera resúmenes con un LLM, priorizando **decisiones, eventos y tareas asignadas**, de dos formas:

- **Bajo demanda**, opcionalmente acotado a un `timeInterval`.
- **Automáticamente** cuando un usuario entra a un canal con más de `MESSAGE_THRESHOLD` mensajes nuevos sin resumir.

## Lenguaje ubicuo

| Término | Significado |
| --- | --- |
| `summary` | Resumen de un canal: estado, contenido (`content`, texto) y `structured` (campos) |
| `digest` | Resultado inmutable del LLM para una solicitud, con la trazabilidad de su ventana; 1:1 con `summary` |
| `checkpoint` | Cursor por canal del último resumen incremental confirmado (no es lectura por usuario) |
| `LocalMessage` | Copia local reconstruible de los mensajes del chat; la fuente de verdad es `messages` |
| `timeInterval` | Rango `[start, end)` pedido para acotar los mensajes |
| `messageThreshold` | Mensajes nuevos que, superados, gatillan un resumen automático |
| `messageState` | `pendiente` → `procesando` → `completada` \| `fallida` |

## Frontera de responsabilidad

- Es dueño exclusivo de `summary`, `digest` y `checkpoint` (más el historial local y las entradas a canales), en su propia base `summaries-mongodb`.
- **Nunca** accede a las bases de `users`, `channels` ni `messages`: solo a sus APIs públicas y a los eventos del broker. Puede guardar IDs de esas entidades, pero no es su dueño.
- No se modifican archivos del repositorio del curso.

## Arquitectura

**Asíncrono con polling.** `POST /api/v1/channels/{id}/summaries` no espera al LLM: valida el canal, guarda la solicitud junto con su evento `summary.requested` y responde `202` + `summary_id`. El cliente consulta `GET /api/v1/summaries/{id}` hasta `completada` o `fallida`.

```text
POST → Mongo (solicitud + evento) → RabbitMQ summary.requested → inbox → worker → LLM → digest + estado + checkpoint
                                                                                    └→ summary.generated | summary.failed
```

Piezas (`app/`): `routes/` (REST), `services.py` (solicitudes, ventanas, entradas), `worker.py` (generación con lease y reintentos), `events.py` (hilo de RabbitMQ: consumo y outbox), `repository.py` (PyMongo), `clients/core_client.py` (users/channels/messages) y `clients/llm_client.py` (mock y Gemini), `event_schemas.py` (esquemas de eventos), `models.py`, `errors.py`.

| API expuesta | Función |
| --- | --- |
| `POST /api/v1/channels/{id}/summaries` | Solicitar un resumen (202) |
| `GET /api/v1/summaries/{id}` | Estado y contenido (polling) |
| `GET /api/v1/channels/{id}/summaries` | Listar (paginado) |
| `GET /api/v1/channels/{id}/summaries/latest` | Último completado |
| `GET /api/v1/events` | Eventos durables del servicio |

| Evento | Dirección |
| --- | --- |
| `chat.message.created` | Consume (historial local) |
| `summary.requested` | Publica y consume |
| `summary.generated` / `summary.failed` | Publica |
| `chat.channel.entered` | Consume (lo publicaría un Gateway) |

## Decisiones vigentes

- **Entrega al menos una vez** con outbox/inbox dentro del documento del resumen: el evento se guarda en la misma escritura que el cambio que lo origina; los consumidores deduplican.
- **Lease con dueño** para el worker; si se cae, otro retoma la solicitud sin repetir la llamada al LLM si el digest ya existe.
- **Salida estructurada**: el LLM devuelve JSON validado con Pydantic (`StructuredSummary`); el texto `content` lo arma el código.
- **Ventana al LLM**: 7 días, 500 mensajes y 400 000 caracteres, desde los más recientes; lo omitido se registra y, en incrementales, se da por cubierto.
- **LLM**: Gemini es obligatorio (sin `LLM_API_KEY` el servicio no arranca); el proveedor `mock` es solo para pruebas automáticas. Modelo principal `gemini-3.5-flash-lite` y respaldo `gemini-3.8-flash` (cuota gratuita informada: 15 RPM/500 RPD y 5 RPM/20 RPD). Sin proveedor de pago.
- **Un solo broker**: `summaries-rabbitmq`, compartido con `messages` mediante un *override* de `RABBITMQ_URL` (README).
- **pymongo síncrono** (como el curso), `httpx`, `pika`; Python 3.12; puerto host 8004.
- **Secretos**: `LLM_API_KEY` solo en `.env` (ignorado por git y por Docker), **nunca** en `.env.example`, que está versionado.
- **Docker**: imagen no root con healthcheck y sin `--reload`; desarrollo con `docker-compose.dev.yaml`.

## Hallazgos del curso que afectan al servicio

- **Bug del demo**: sus `POST` de creación responden 500 aunque guardan el dato, y `messages` **nunca publica `chat.message.created`** (no puede serializar el `_id` de Mongo). Por eso el servicio reconcilia el historial con `GET .../messages` en cada ventana; no es un respaldo menor. No se tocó el código del curso; conviene avisar al profesor.
- `messages` solo publica con `RABBITMQ_URL`, que su compose no define; el exchange `chat` es topic **no durable** y debe declararse igual.
- `GET .../messages` devuelve todo el canal sin paginar ni filtrar por fecha, y `[]` aunque el canal no exista.
- No existe Gateway del chat. El del demo de la Unidad 4 es de otro dominio y no usa el broker. Detalle en `docs/REFERENCIA.md`.

## Limitaciones conocidas

- Sin autenticación (el chat base tampoco la tiene).
- `chat.channel.entered` hoy se prueba con `python -m app.tools.publish_channel_entered`; no hay un productor real.
- Las capas gratuitas de Gemini pueden usar los mensajes enviados para mejorar sus modelos y el respaldo también puede saturarse (503): usar datos ficticios.

## Cómo trabajar aquí

- Pruebas (solo Docker, ~260, usan Mongo y RabbitMQ reales y simulan el núcleo y Gemini):
  `docker compose -p summaries-tests -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests --attach tests`, y luego `down -v`.
- Si cambia un modelo de evento: `python -m app.tools.export_event_schemas` (una prueba falla si `docs/eventos/` se desincroniza).
- Si cambia un contrato, el lenguaje ubicuo o la frontera de responsabilidad, actualizar este archivo y `docs/REFERENCIA.md`.
- Rama estable `main`; el trabajo va en ramas `feature/<nombre>` con PR.
