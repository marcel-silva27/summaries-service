# Verificación

Qué se comprobó y cómo. Los comandos para repetirlo están en el [README](../README.md).

## Pruebas automáticas

**266 pruebas aprobadas** con MongoDB 7 y RabbitMQ reales (stack aislado de `compose.test.yaml`); el núcleo del chat y Gemini se simulan (`httpx.MockTransport`), así que no usan cuota ni datos reales.

| Área | Qué cubren |
| --- | --- |
| Persistencia | Estados e invariantes de los modelos, índices, concurrencia con ocho hilos (un solo claim, un solo digest), replays idempotentes, aislamiento de canales, empates de fecha |
| Ventanas y cobertura | Más de 1000 mensajes, mensajes tardíos, snapshot inmutable, límites de 7 días / 500 mensajes / tamaño, intervalos que terminan en el futuro |
| API | Códigos y formato de errores, respuestas que no exponen campos internos, `Location`, paginación |
| Mensajería | Outbox y inbox con RabbitMQ real, publicación fallida y recuperación, eventos duplicados e inválidos (dead letter), reconexión |
| Worker | Lease con dueño, reintentos, recuperación tras caída, un solo digest por solicitud, cobertura recuperable |
| LLM | Mock; Gemini con transporte simulado (429 y espera corta, modelo de respaldo, errores definitivos, esquema inválido) y salida estructurada |
| Eventos | Cada evento emitido de verdad valida contra su esquema Pydantic y contra el JSON Schema de `docs/eventos/`; los archivos están sincronizados con los modelos |
| OpenAPI | Documento válido (`openapi-spec-validator`), todas las operaciones con descripción y errores documentados, ejemplos que son instancias válidas |
| Chat simulado | Cumple los contratos estrictos del cliente del núcleo, sus POST responden 201, publica `chat.message.created` y `chat.channel.entered` válidos (también contra RabbitMQ real) |
| Resumen automático | Umbral estricto (50 no, 51 sí), una solicitud activa por canal con varios workers, idempotencia por `entry_id`, referencias inválidas, ventana de 7 días, flujo completo por el broker |

## Pila real (chat del curso + este servicio)

Verificado con el demo de la Unidad 3 en ejecución y el override de `RABBITMQ_URL`.

| Escenario | Resultado |
| --- | --- |
| RabbitMQ caído al pedir resúmenes | `202`; quedan `pendiente`; al volver el broker se completan solos (~4 s) |
| RabbitMQ recreado (contenedor nuevo, mismo volumen) | La solicitud en vuelo se completa |
| Mongo caído | `POST` y `GET` responden `503`, `/health` sigue ok; al volver, el servicio se recupera sin reiniciar |
| Servicio reiniciado con solicitudes pendientes y broker caído | Al volver el broker se completan todas |
| Clon limpio del repositorio siguiendo el README | Arranca, imagen no root saludable, API y `/docs` responden y el resumen se completa con `structured` |
| Sin `LLM_API_KEY` | El servicio se niega a arrancar con un mensaje que indica cómo configurarla |
| Salida estructurada con Gemini real (desde el clon, con `.env`) | `completada` en ~2,5 s; `decisions`, `events` (con `when`) y `tasks` (con `assignee`) correctos; `content` armado por el código |
| Resumen por intervalo con Gemini real | `pendiente → procesando → completada` con decisiones, tareas con responsable y eventos correctos |
| Resumen automático con Gemini real | El mismo `chat.channel.entered` publicado dos veces abre **una** solicitud, que se completa |
| Servicio matado a mitad de la llamada a Gemini | El lease vence y otro intento la completa: un solo digest y un solo `summary.generated` |
| Intervalo sin mensajes | `fallida` («No hay mensajes en la ventana solicitada»), sin llamar al LLM |
| Canal o usuario inexistente | `404` / `422`, sin crear solicitud ni evento |

Observaciones: Flash-Lite responde normalmente en 1,5–2 s (una vez tardó 15 s, sin reproducirse); el modelo de respaldo `gemini-3.8-flash` devolvió un 503 en una llamada directa, así que también puede saturarse. Cada intento a Gemini queda en el log con modelo, duración y motivo.

Los datos de prueba creados en el demo del curso se borraron después de cada ejecución.

## Defectos encontrados al probar

- El límite de 7 días se contaba desde el fin del intervalo pedido, y uno con fin en el futuro dejaba `fallida` un resumen válido. Corregido: ahora se cuenta desde el menor entre el fin del intervalo y el momento de la solicitud.
- Las caídas de broker o Mongo generaban una traza completa por segundo en el log. Ahora es una línea por intento.
- El demo del curso devuelve 500 en sus `POST` de creación y nunca publica `chat.message.created` (ver [REFERENCIA](./REFERENCIA.md#hallazgos-sobre-el-código-del-curso)).

## Recorrido completo con la demo (chat simulado + Gemini real)

Con `docker-compose.demo.yaml`: 20 comprobaciones sobre todos los endpoints, todas correctas — resumen incremental con `user_id` (`pendiente → procesando → completada` en ~2,4 s, con decisiones, eventos y tareas con responsable), `GET` por id, listado, `latest`, resumen por `timeInterval`, resumen automático disparado desde `POST /channels/{id}/enter` del Gateway simulado (y una segunda entrada sin mensajes nuevos que no crea nada), los errores documentados (404, 422 por usuario, por intervalo invertido y por `limit=0`), eventos con paginación, `/health`, `/openapi.json` y `/docs`. Sin trazas en los logs; cada llamada a Gemini tardó 1,2–2,4 s.
