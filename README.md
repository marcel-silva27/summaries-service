# Servicio de Resúmenes

Capacidad vertical para la cátedra de Microservicios. Genera resúmenes automáticos (vía LLM) sobre las conversaciones del chat base del curso ([rmasad/microservicios](https://github.com/rmasad/microservicios)), sin acceder a sus bases de datos: toda interacción ocurre por sus APIs públicas o por eventos del broker.

Detalle de dominio, contratos y decisiones de arquitectura: ver [`CLAUDE.md`](./CLAUDE.md) y la [propuesta completa](https://claude.ai/artifact/BSeEEVB7yRPouL1wqsg497).

## Estado y alcance

Etapas **1 a 6 completadas**: FastAPI, persistencia Mongo, cliente HTTP, ventanas reconciliadas, REST con polling e integración RabbitMQ.

Las cuatro rutas de summaries ya funcionan. POST valida referencias y devuelve `202` con una solicitud durable; RabbitMQ la publica y registra su recepción. **Queda en pendiente hasta implementar el worker/LLM del paso 7**. No se simula una generación exitosa. `/api/v1/events` sigue siendo un espejo en memoria; el evento requested sí se conserva en Mongo.

Decisiones en [`CLAUDE.md`](./CLAUDE.md). Conceptos, contratos y detalles reunidos en [`docs/REFERENCIA.md`](./docs/REFERENCIA.md).

Guía paso a paso para ejecutar el entorno y probar el avance desde PowerShell: [`INSTRUCCIONES.md`](./INSTRUCCIONES.md).

## Estructura

```text
summaries-service/
├── CLAUDE.md                 # Dominio, contratos y decisiones vigentes
├── README.md                 # Ejecución, pruebas y próximos pasos
├── INSTRUCCIONES.md          # Guía práctica de ejecución y pruebas en PowerShell
├── docs/REFERENCIA.md        # Glosario y referencia técnica global
├── docker-compose.yaml       # Servicio, Mongo y RabbitMQ con volúmenes propios
├── compose.test.yaml         # Pruebas aisladas con MongoDB 7 y RabbitMQ reales
├── Dockerfile
├── Dockerfile.test
├── requirements.txt
├── requirements-test.txt
├── tests/                    # Persistencia, cliente HTTP y regresión de FastAPI
└── app/
    ├── main.py               # Lifespan de Mongo/HTTP, dependencias, health y events
    ├── config.py             # Variables de entorno
    ├── models.py             # Summary, Digest, Checkpoint y LocalMessage
    ├── repository.py         # PyMongo síncrono y dependencia para FastAPI
    ├── services.py           # Orquestación REST, reconstrucción y snapshots de ventanas
    ├── errors.py             # Errores 404, 409, 422, 502 y 503
    ├── events.py             # RabbitMQ: outbox requested, consumo continuo y cola de errores
    ├── routes/summaries.py   # POST 202, polling, listado y latest
    └── clients/
        ├── core_client.py    # Cliente único de users/channels/messages
        └── llm_client.py     # Cliente LLM, pendiente
```

## Requisitos

- Docker y Docker Compose
- El chat base del curso corriendo (ver más abajo)

## Cómo correrlo

1. Clona el repo del curso en una carpeta aparte (no la mezcles con este repo) y levanta el chat base — esto crea la red `microsvcs`:

   ```bash
   git clone https://github.com/rmasad/microservicios.git
   cd microservicios/03_Comunicación/demo
   docker compose up --build
   ```

2. En otra terminal, levanta este servicio — se conecta a la red ya creada en el paso 1:

   ```bash
   cd summaries-service
   docker compose up --build
   ```

3. Conecta `messages` al broker elegido. Desde PowerShell, sustituye la ruta del compose del curso:

   ```powershell
   $coreCompose = '<ruta-al-chat-base>/docker-compose.yaml'
   @'
   services:
     messages:
       environment:
         RABBITMQ_URL: amqp://summaries-rabbitmq:5672
   '@ | docker compose -p demo -f $coreCompose -f - up -d --no-deps messages
   ```

   Es un override por stdin: no crea archivos ni edita el repo del curso. Ya se aplicó al entorno actual. Repetirlo si se recrea el chat desde su compose original; si el proyecto tiene otro nombre, reemplazar `demo`.

4. Servicio: `http://localhost:8004`; OpenAPI: `http://localhost:8004/docs`; salud: `http://localhost:8004/health`.

Compose espera Mongo y RabbitMQ saludables. FastAPI comprueba conexión/índices de Mongo y administra el cliente HTTP y el hilo de eventos; `/health` conserva `{"status":"ok"}` como liveness. Si RabbitMQ cae después, el publicador reconecta y conserva los requested pendientes en Mongo.

Mongo usa `summaries-data` y RabbitMQ `summaries-rabbitmq-data`; ambos sobreviven a recreaciones. No ejecutar `docker compose down -v` sobre desarrollo si se quieren conservar.

No se editan los archivos de `microservicios/`; solo se configura RABBITMQ_URL en ejecución.

## Flujo de trabajo

- Todo el código y los commits van en `summaries-service/`, nunca en la carpeta del repo del curso.
- `main` como rama estable; features en ramas `feature/<nombre>`, PR contra `main`.
- Antes de tocar un contrato con el núcleo (rutas o payloads de `users`/`channels`/`messages`), revisar su documentación OpenAPI en `localhost:800{1,2,3}/docs` — son la fuente de verdad, no asumir el esquema de memoria.
- Cada endpoint nuevo: actualizar también `CLAUDE.md` si cambia algún contrato, el lenguaje ubicuo o la frontera de responsabilidad.
- El servicio nunca lee ni escribe las bases de datos de `users`, `channels` ni `messages` — solo sus APIs públicas o sus eventos.

## Variables de entorno

| Variable | Descripción |
| --- | --- |
| `MONGODB_URL` | Conexión a `summaries-mongodb` |
| `MONGODB_DATABASE` | Base propia, por defecto `summaries` |
| `MONGODB_TIMEOUT_MS` | Timeout de selección de servidor, conexión y socket; por defecto `5000` ms |
| `USERS_URL` | Base URL del servicio `users` del núcleo |
| `CHANNELS_URL` | Base URL del servicio `channels` del núcleo |
| `MESSAGES_URL` | Base URL del servicio `messages` del núcleo |
| `LLM_API_URL` | Endpoint del proveedor de LLM |
| `BROKER_URL` | Conexión al broker de eventos |
| `OUTBOX_POLL_SECONDS` | Sondeo de respaldo de la outbox (default 5); la publicación normal ocurre por aviso |
| `BROKER_TIMEOUT_SECONDS` | Timeout de conexión/bloqueo AMQP; por defecto `2` s |
| `BROKER_RETRY_SECONDS` | Espera entre reconexiones o reintentos por Mongo caído; por defecto `1` s |
| `CORE_TIMEOUT_SECONDS` | Timeout HTTP al núcleo; por defecto `2` s |
| `LLM_API_KEY` | Credencial del proveedor LLM, cuando se implemente el cliente |
| `LLM_TIMEOUT_SECONDS` | Timeout LLM; por defecto `60` s |
| `MESSAGE_THRESHOLD` | Umbral automático; por defecto `50`; el disparo será con **más de** ese valor |

Compose carga `.env.example` (valores por defecto versionados) y después `.env` si existe, que lo sobrescribe: crea `.env` (ignorado por git) para secretos como `LLM_API_KEY`. MONGODB_URL se fija siempre al contenedor propio; BROKER_URL admite override desde el entorno o `.env`, con default summaries-rabbitmq. LLM_API_URL aún apunta a llm-mock, que se implementará en el paso 7; no versionar credenciales reales.

## Uso de la API

Con IDs reales del núcleo, `curl` (o `curl.exe` en PowerShell):

```bash
curl -i -X POST http://localhost:8004/api/v1/channels/CHANNEL_ID/summaries -H 'Content-Type: application/json' -d '{"user_id":"USER_ID"}'
curl http://localhost:8004/api/v1/summaries/SUMMARY_ID
curl 'http://localhost:8004/api/v1/channels/CHANNEL_ID/summaries?limit=20&offset=0'
curl http://localhost:8004/api/v1/channels/CHANNEL_ID/summaries/latest
```

POST acepta cuerpo vacío, usuario opcional y `timeInterval` con start/end ISO8601 y zona. Devuelve summary_id/messageState y Location. El listado devuelve una lista, con limit 1–1000 y offset ≥ 0. latest devuelve solo completados, o 404. Polling por ID no llama al núcleo; listado/latest validan canal. Errores mantienen `{"detail": ...}` y están declarados en OpenAPI.

## Pruebas

Requieren solo Docker; el compose de pruebas levanta Mongo/RabbitMQ aislados. No necesitan chat base, LLM ni Python instalado en el host. Desde `summaries-service/`:

```bash
docker compose -p summaries-tests -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests --attach tests
docker compose -p summaries-tests -f compose.test.yaml down -v
```

El primer comando retorna el resultado de pytest; el segundo limpia solo el stack de pruebas. Mongo/RabbitMQ reales y HTTP simulado con MockTransport, sin modificar el núcleo.

Alternativa con Python **3.12**: instalar requirements-test.txt, definir TEST_MONGODB_URL y TEST_BROKER_URL para instancias aisladas y ejecutar `python -m pytest -q tests`. Las pruebas de infraestructura fallan explícitamente si faltan esas variables.

Verificación actual: **166 pruebas aprobadas**. Incluyen REST → Mongo → RabbitMQ → bandeja recibida, publicación fallida/recuperación, mensajes inválidos, ack/requeue, replays, ventanas de más de 1000 mensajes y tardíos durante una generación. Los avisos de deprecación de Starlette/Pika no afectan el resultado.

También se verificó la persistencia al recrear Mongo/FastAPI, la conexión de messages al broker compartido y la conservación de una cola/mensaje persistentes al recrear RabbitMQ, con reconexión de consumidores. Historial vacío no valida existencia del canal: usar get_channel.

## Próximos pasos hasta la entrega final

| Etapa | Implementación | Criterio de aceptación |
| --- | --- | --- |
| 1 — completada | Esqueleto FastAPI, Docker y configuración | Servicio disponible en 8004 y contratos OpenAPI declarados |
| 2 — completada | Modelos y repositorio Mongo propio | Cuatro colecciones, índices, idempotencia y concurrencia verificadas |
| 3 — completada | Cliente HTTP del núcleo | Contratos verificados, conexiones compartidas, timeout y errores 404/422/502/503; sin DB ajenas |
| 4 — completada | Ventanas y reconstrucción por API | Ventanas completas/paginadas, snapshot fijo y cobertura de mensajes tardíos |
| 5 — completada | REST y polling | POST 202 durable, GET local, listado paginado y latest completado |
| 6 — completada | RabbitMQ y consumo continuo | Broker compartido, outbox/inbox requested, ack manual, replays y cola de errores |
| 7 — siguiente | Worker y LLM HTTP | Resúmenes reales, fallas explícitas y recuperación de trabajos |
| 8 | Eventos de resultado fiables | generated/failed durables y notificables al Gateway |
| 9 | Entrada automática por Gateway | Evento de entrada, umbral estricto y deduplicación por canal |
| 10 | Integración y entrega | End2end real, resiliencia y repositorio público documentado |

### Paso 7 — worker y LLM HTTP

- Crear un worker independiente del hilo RabbitMQ. Leer received_requests de Mongo y reclamar cada pendiente con exclusión mutua; el consumo del historial debe continuar mientras el LLM responde.
- Implementar lease/ownership para recuperar procesando tras una caída y evitar que un worker vencido confirme resultados. Reutilizar el snapshot guardado y cualquier digest ya persistido; no volver a llamar al LLM si el resultado existe.
- Completar llm_client.py: POST a LLM_API_URL/generate, credencial opcional, timeout y validación de contenido no vacío. Resolver nombres de autores con caché; el prompt debe priorizar decisiones, eventos y responsables de tareas sin inventar información.
- Usar el contrato HTTP propuesto en REFERENCIA.md y agregar el mock al compose de pruebas. La entrega final debe poder apuntar a una API real; el mock no será el proveedor de producción.
- Guardar digest → completar summary → confirmar cobertura incremental/checkpoint. Ventanas por intervalo no consumen la cobertura del canal. Ventana vacía, errores de proveedor o respuesta inválida deben terminar en fallida con una explicación consultable por polling.
- Para conversaciones que superen el contexto del proveedor, procesar partes sin truncar mensajes silenciosamente y producir un digest final por solicitud. Persistir avances necesarios para recuperar una ejecución interrumpida.

**Se acepta cuando:** POST sigue sin esperar al LLM, polling alcanza completada/fallida, contenido y cobertura coinciden con el snapshot, y reinicios/concurrencia no producen dos confirmaciones ni pierden mensajes.

### Paso 8 — eventos de resultado y entrega fiable

- Extender la outbox existente a summary.generated y summary.failed; guardar el evento junto al cambio terminal del summary, en una escritura del mismo documento.
- Publicar desde el hilo RabbitMQ con mensajes persistentes, confirmación y reintento. Mantener una identidad estable por evento y aceptar entrega al menos una vez; el consumidor debe deduplicar sus efectos.
- Recuperar las confirmaciones de cobertura pendientes si el proceso cae entre Mongo y la publicación. Una falla del broker no debe borrar un resumen completado ni volver a invocar al LLM.
- Hacer que GET /api/v1/events recupere los eventos durables después de reiniciar; hoy es solo el espejo en memoria. Registrar intentos/errores de entrega para diagnóstico.

**Se acepta cuando:** cada resumen terminal produce su evento correcto, una caída en cada punto de confirmación se recupera, y un Gateway consumidor puede recibir el resultado sin consultar bases ajenas.

### Paso 9 — entrada al canal publicada por Gateway

- Implementar/conectar el Gateway: al entrar un usuario a un canal publicará chat.channel.entered con channel_id, user_id y entry_id. El contrato propuesto está en REFERENCIA.md; ese evento todavía no se consume.
- Agregar una cola durable propia para entradas. Validar referencias y deduplicar por entry_id; no reutilizar las colas de otros servicios.
- Antes de comprobar el umbral, reconciliar por la API pública para recuperar eventos que el núcleo no haya publicado. Incluir en messages_since los tardíos aún sin cobertura.
- Si messages_since **> MESSAGE_THRESHOLD**, crear una solicitud automática por la misma orquestación durable del POST. Con 50 mensajes y umbral 50 no se genera; con 51, sí.
- Evitar más de una solicitud automática activa por canal aunque entren varios usuarios a la vez. Reintentar una entrada duplicada no debe abrir otra solicitud; liberar/recuperar la exclusión al completar, fallar o vencer su lease.
- El Gateway consumirá generated/failed para avisar al cliente; el contenido final sigue consultándose por GET/polling. Las solicitudes manuales con timeInterval mantienen su independencia del progreso incremental.

**Se acepta cuando:** un evento real del Gateway inicia la generación solo al superar el umbral; entradas simultáneas/repetidas, mensajes tardíos y caídas no duplican solicitudes ni alteran resúmenes publicados.

### Paso 10 — integración, resiliencia y entrega

- Probar el recorrido con chat base, broker compartido, Gateway y una API LLM real configurada: crear conversación, pedir un intervalo y verificar contenido; luego superar el umbral y entrar al canal para verificar la generación automática.
- Cubrir desconexión/recreación de RabbitMQ, Mongo temporalmente caído, reinicio del worker durante una llamada, eventos duplicados/inválidos, timeouts LLM y canales que requieren varias partes.
- Verificar que usuarios/canales inexistentes no crean solicitudes, que solo se usan APIs/eventos del núcleo y que las respuestas públicas no exponen metadatos internos ni credenciales.
- Actualizar ejemplos reproducibles, variables reales de despliegue, diagramas y límites operativos. Preparar rama estable, PR y repositorio público según las reglas de entrega; no incluir secretos.

**Se acepta cuando:** todo el flujo manual y automático funciona con componentes reales, las pruebas de resiliencia pasan y otra persona puede reproducirlo siguiendo el README.

## Decisiones de arquitectura resueltas por el usuario

| Tema | Elección | Estado |
| --- | --- | --- |
| Broker | summaries-rabbitmq compartido con messages | Configurado y conexión comprobada |
| LLM | API HTTP configurable + mock solo para pruebas | Elegido; implementación en paso 7 |
| Entrada al canal | Evento publicado por un Gateway | Elegido; integración en paso 9 |
| Mensajes tardíos | Próximo incremental, sin reescribir resúmenes publicados | Implementado y probado |

Para el despliegue final faltarán la URL/credencial de la API LLM real y el Gateway que emitirá la entrada. Estas configuraciones no cambian las decisiones elegidas.
