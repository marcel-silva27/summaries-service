# Pruebas del servicio de resúmenes (Unidad 7: control de calidad)

Documenta qué se prueba, cómo se aísla cada prueba y cómo ejecutarlas. Todo corre solo con Docker.

| Nivel | Dónde | Qué usa | Comando |
| --- | --- | --- | --- |
| Servicio y unitarias | `tests/` | Mongo y RabbitMQ reales y desechables; núcleo y Gemini simulados | `compose.test.yaml` |
| Contrato | `tests/test_core_contract.py`, `tests/test_contracts.py` y `tests_integration/test_core_contract.py` | Fixtures reales del núcleo en `tests/contracts/` y esquemas de `docs/eventos/` | `tests/` e integración |
| Integración | `tests_integration/` | `users`, `channels` y `messages` **reales** del curso, más este servicio | `compose.integration.yaml` |

Última ejecución: `tests/` → 325 pruebas pasaron; `tests_integration/` → 12 pasaron.

## Cómo ejecutar

Servicio, contrato y unitarias:

```bash
docker compose -p summaries-tests -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests --attach tests
docker compose -p summaries-tests -f compose.test.yaml down -v
```

Integración con el chat base real (la primera vez descarga el código del curso, fijado al commit `8930ac9`):

```bash
python tools/prepare_peers.py core
docker compose -p summaries-integration -f compose.integration.yaml up --build --abort-on-container-exit --exit-code-from integration-tests --attach integration-tests
docker compose -p summaries-integration -f compose.integration.yaml down -v
```

Ambos stacks son autónomos: red y volúmenes propios, sin puertos publicados al host, y no tocan los datos del chat ni de `docker-compose.yaml`.

## Aislamiento de la base de datos

- **Pruebas de servicio** (`tests/conftest.py`): la fixture `repository` crea una base `test_summaries_<uuid>` en un MongoDB desechable y la elimina al terminar. El nombre nunca viene del entorno, por lo que no puede apuntar a datos reales.
- **El núcleo del chat** se simula con `httpx.MockTransport` (fixture `api`): las pruebas no usan `users`, `channels` ni `messages`, ni acceden a sus bases.
- **RabbitMQ**: la fixture `broker` usa colas con prefijo único y las borra al terminar.
- **Integración**: cada prueba crea usuario y canal propios con sufijos aleatorios, así que no depende del orden ni de datos previos. El stack se destruye con `down -v`.

## Pruebas por endpoint

Cada endpoint tiene sus propias pruebas en `tests/test_endpoints.py`, con al menos un caso válido y uno de error. Las pruebas llevan el nombre del endpoint (`test_post_summary_...`, `test_get_summary_...`, `test_list_summaries_...`, `test_latest_summary_...`, `test_events_...`, `test_health_...`).

| Endpoint | Casos válidos | Casos de error |
| --- | --- | --- |
| `POST /api/v1/channels/{id}/summaries` | 202 con `Location` y estado `pendiente`; acepta `timeInterval` | Canal inexistente 404 (no guarda nada); usuario inexistente 422; núcleo caído 503; cuerpo inválido 422 |
| `GET /api/v1/summaries/{id}` | Resumen completado con su contenido; resumen fallido con su `error` | Id inexistente 404 |
| `GET /api/v1/channels/{id}/summaries` | Todos los estados del canal, sin mezclar otros canales; paginación con `limit` y `offset` | Canal inexistente 404; paginación inválida 422 |
| `GET /api/v1/channels/{id}/summaries/latest` | Devuelve el completado e ignora los pendientes | Sin completados 404; canal inexistente 404 |
| `GET /api/v1/events` | Lista `summary.requested` tras un `POST`; paginación | `limit` u `offset` inválidos 422 |
| `GET /health` | `200 {"status": "ok"}` | `POST` responde 405 |

Los errores usan el formato uniforme `{"detail": ...}`, verificado en `tests/test_contracts.py`. Las pruebas más antiguas de `tests/test_app.py` siguen cubriendo combinaciones y casos límite.

## Contratos

Un contrato tiene dos lados y aquí se prueban los dos. Las fixtures de `tests/contracts/` son **respuestas reales** del chat base (commit `8930ac9` del curso, ver `_meta` en `core_http.json`): cada una guarda la petición y la respuesta que este servicio espera de `users`, `channels` y `messages`, más el evento `chat.message.created`.

| Lado | Prueba | Qué falla si algo cambia |
| --- | --- | --- |
| Consumidor (este servicio) | `tests/test_core_contract.py` | Si el servicio deja de entender las respuestas grabadas, o pide una ruta que no está en el contrato. No levanta el núcleo ni toca sus bases. |
| Proveedor (el núcleo real) | `tests_integration/test_core_contract.py` | Si `users`, `channels` o `messages` cambian una ruta, un código de estado, un campo o un tipo, o si dejan de publicar el evento en el broker. |

Detalles:
- La verificación del proveedor compara la **forma** (claves y tipos), no los valores: los IDs y las fechas cambian en cada ejecución. Probé que detecta un campo renombrado, uno extra y un tipo distinto.
- El evento se lee desde RabbitMQ, igual que lo recibe este servicio, y no desde el endpoint `/events` del núcleo.
- Quedan registradas particularidades del núcleo: `messages` responde `200 []` para un canal inexistente, y el evento lleva el mensaje completo en `data`.
- Para actualizar las fixtures si el núcleo cambia de forma legítima: levantar `compose.integration.yaml`, repetir las llamadas, reemplazar los IDs por los fijos de `_meta.ids` y ajustar el commit en `_meta`.

Los contratos que este servicio **publica** (`summary.requested`, `summary.generated`, `summary.failed`) se validan contra los modelos Pydantic y contra `docs/eventos/`; una prueba falla si se desincronizan (se regeneran con `python -m app.tools.export_event_schemas`). El OpenAPI también se valida: operaciones documentadas, `operationId` únicos y errores con el esquema uniforme (`tests/test_contracts.py`).

## Integración (`tests_integration/`)

Las cinco primeras pruebas están en `test_flow.py`. Usa el código real de `users`, `channels` y `messages` del curso y este servicio con su Mongo y RabbitMQ. **El LLM es el proveedor `mock`**: la prueba verifica el flujo entre servicios, no la calidad del modelo, y no consume la cuota de Gemini. La integración con Gemini se prueba en `tests/test_gemini.py`.

| Prueba | Qué demuestra |
| --- | --- |
| `test_summary_of_real_chat_messages_is_generated_end_to_end` | Crear usuario, canal y mensajes en el núcleo real, solicitar el resumen (202), hacer polling hasta `completada` y consultar `latest` |
| `test_the_summary_events_are_emitted_for_the_transaction` | `summary.requested` y `summary.generated` quedan en `/api/v1/events` |
| `test_unknown_channel_is_rejected_using_the_real_channels_service` | Canal inexistente → 404 |
| `test_unknown_user_is_rejected_using_the_real_users_service` | Usuario inexistente → 422 |
| `test_core_message_events_reach_the_summaries_broker` | `messages` registra `chat.message.created` con su `channel_id` |
| `test_core_contract.py` (7 pruebas) | Verificación del proveedor del contrato: respuestas HTTP y evento en el broker, ver «Contratos» |

## Qué no cubre

- No es Pact: las fixtures las mantiene este repositorio y el equipo del chat base no ejecuta el contrato en su CI. La verificación del proveedor corre aquí, contra su código fijado.
- No se prueba la interfaz web en el navegador. `tools/record_demo.py` la graba a mano contra el stack completo.
