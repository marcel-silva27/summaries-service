# Guía paso a paso (PowerShell)

Esta guía es para **PowerShell**, desde la carpeta `summaries-service/`. Se necesita Docker Desktop iniciado y Docker Compose; no hace falta Python. En Linux/macOS, los comandos equivalentes están en el [README](./README.md).

El servicio está completo: API con polling, worker, eventos de resultado y resumen automático. Por defecto usa el proveedor `mock` (resumen **simulado**); para Gemini real, ver el README (sección *Configurar Gemini*).

## 1. Ejecutar las pruebas automáticas, sin levantar el chat

Esta es la forma más rápida de comprobar que todo funciona. Levanta MongoDB y RabbitMQ independientes de los del proyecto; las llamadas HTTP al núcleo se simulan.

```powershell
docker compose -p summaries-tests -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests --attach tests
$codigoPruebas = $LASTEXITCODE
docker compose -p summaries-tests -f compose.test.yaml down -v
"Codigo de pruebas: $codigoPruebas (0 = correcto)"
```

Resultado esperado: **227 passed**, código 0. Pueden aparecer avisos de deprecación de Starlette/Pika sin que fallen las pruebas. La limpieza con `-v` se aplica únicamente al proyecto aislado `summaries-tests`.

La suite comprueba API, persistencia, errores, concurrencia, ventanas de más de 1000 mensajes, tardíos, publicación/recepción RabbitMQ, replays y cola de errores. El LLM y el núcleo se simulan: estas pruebas no usan cuota de Gemini.

## 2. Levantar el entorno completo

### 2.1. Preparar rutas

Ubícate en este repositorio y reemplaza la ruta del chat base por la de tu equipo. El repositorio del curso debe estar en una carpeta aparte.

```powershell
Set-Location 'C:\ruta\summaries-service'
$rutaServicio = (Get-Location).Path
$rutaChat = 'C:\ruta\microservicios\03_Comunicación\demo'
$composeChat = Join-Path $rutaChat 'docker-compose.yaml'
```

Si necesitas obtener el chat base, su repositorio es https://github.com/rmasad/microservicios.

### 2.2. Levantar users, channels y messages

```powershell
docker compose -p demo -f $composeChat up -d --build
docker network inspect microsvcs
```

El chat crea la red `microsvcs`. Este servicio la utiliza como red externa; no hay que crear una red adicional.

### 2.3. Levantar summaries, su Mongo y su RabbitMQ

```powershell
Set-Location $rutaServicio
docker compose up -d --build --wait --wait-timeout 120
docker compose ps
```

Mongo y RabbitMQ deben aparecer saludables; summaries debe estar iniciado. Compose carga `.env.example` (valores por defecto) y, si existe, `.env` encima. Para esta guía no necesitas crear `.env` ni una API key de LLM.

### 2.4. Conectar messages al mismo broker

```powershell
@'
services:
  messages:
    environment:
      RABBITMQ_URL: amqp://summaries-rabbitmq:5672
'@ | docker compose -p demo -f $composeChat -f - up -d --no-deps messages
```

Este override configura el contenedor sin editar archivos del curso. Repítelo si recreas `messages` con el compose original. La guía usa el proyecto `demo`; si tu chat ya está iniciado con otro nombre de proyecto, usa ese nombre también en estos comandos para evitar levantar otro chat.

### 2.5. Comprobar salud y documentación

```powershell
Invoke-RestMethod 'http://localhost:8001/health'
Invoke-RestMethod 'http://localhost:8002/health'
Invoke-RestMethod 'http://localhost:8003/health'
Invoke-RestMethod 'http://localhost:8004/health'
```

Cada servicio debe devolver status=ok. `/health` de summaries confirma que la aplicación atiende; la actividad del broker se revisa en el apartado 4.

| Servicio | OpenAPI / Swagger |
| --- | --- |
| Users | http://localhost:8001/docs |
| Channels | http://localhost:8002/docs |
| Messages | http://localhost:8003/docs |
| Summaries | http://localhost:8004/docs |

## 3. Probar la API de summaries

Mantén la misma sesión PowerShell para reutilizar las variables.

### 3.1. Obtener un canal existente

```powershell
$canales = Invoke-RestMethod 'http://localhost:8002/api/v1/channels'
$canales | Format-Table id, name
```

Si la lista está vacía, crea un canal desde Swagger de channels, usando POST `/api/v1/channels` con `{"name":"Pruebas de resumenes"}`, y vuelve a ejecutar el listado. Copia un ID real:

```powershell
$canalId = 'REEMPLAZAR_POR_ID_REAL_DEL_CANAL'
$base = 'http://localhost:8004'
Invoke-RestMethod "http://localhost:8002/api/v1/channels/$canalId"
```

No necesitas un usuario para solicitar un resumen sin user_id. Para preparar conversación, puedes usar Swagger de users para crear un usuario (`display_name`) y Swagger de messages para publicar mensajes en el canal (`author_id`, `content`). Usa IDs devueltos por el núcleo y comprueba sus respuestas antes de continuar.

### 3.2. Solicitar un resumen

```powershell
$respuesta = Invoke-WebRequest -UseBasicParsing -Method Post `
    -Uri "$base/api/v1/channels/$canalId/summaries" `
    -ContentType 'application/json' -Body '{}'
$solicitud = $respuesta.Content | ConvertFrom-Json
$summaryId = $solicitud.summary_id
$respuesta.StatusCode
$respuesta.Headers['Location']
$solicitud | ConvertTo-Json
```

Esperado: HTTP **202**, Location con `/api/v1/summaries/<id>` y cuerpo con summary_id y messageState=pendiente. La solicitud se guarda aunque RabbitMQ esté temporalmente caído; las validaciones del canal/usuario sí requieren que el núcleo responda.

Para pedirlo a nombre de un usuario existente, sustituye el cuerpo por un hashtable serializado: `@{ user_id = 'ID_REAL_DEL_USUARIO' } | ConvertTo-Json`.

### 3.3. Consultar por polling, listar y pedir latest

```powershell
Invoke-RestMethod "$base/api/v1/summaries/$summaryId" | ConvertTo-Json -Depth 6
Invoke-RestMethod "$base/api/v1/channels/$canalId/summaries?limit=20&offset=0" | ConvertTo-Json -Depth 6
curl.exe -i "$base/api/v1/channels/$canalId/summaries/latest"
```

| Consulta | Resultado esperado ahora |
| --- | --- |
| GET por summaryId | 200. `pendiente` → `procesando` → `completada` en unos segundos, con `content` (simulado con `mock`, real con Gemini) |
| Listado del canal | 200, lista con las solicitudes guardadas |
| latest | 404 mientras no haya un resumen completado; después, 200 con el último |

Repite el GET hasta que `messageState` sea `completada` o `fallida` (con `error`). Si el canal no tiene mensajes dentro de la ventana, queda `fallida` con *No hay mensajes en la ventana solicitada*. El listado usa limit de 1 a 1000 y offset desde 0.

### 3.4. Solicitar un intervalo

Este ejemplo pide la última hora; no necesita que esa hora contenga mensajes para aceptar la solicitud.

```powershell
$fin = (Get-Date).ToUniversalTime()
$cuerpoIntervalo = @{
    timeInterval = @{
        start = $fin.AddHours(-1).ToString('o')
        end = $fin.ToString('o')
    }
} | ConvertTo-Json -Depth 4
Invoke-RestMethod -Method Post `
    -Uri "$base/api/v1/channels/$canalId/summaries" `
    -ContentType 'application/json' -Body $cuerpoIntervalo
```

Esperado: 202 y otro summary_id. Start debe ser anterior a end y las fechas deben incluir zona. La selección de mensajes usa `[start,end)`.

### 3.5. Comprobar errores desde Swagger

| Prueba | Esperado |
| --- | --- |
| GET de un summary_id inexistente | 404 |
| POST con canal inexistente | 404, sin crear solicitud |
| POST con user_id inexistente en un canal válido | 422, sin crear solicitud |
| POST con start ≥ end o fecha inválida | 422 |
| Listado con limit=0 u offset=-1 | 422 |

Los errores de negocio usan `{"detail":"..."}`; los de validación de FastAPI pueden incluir una lista en detail. Los detalles de los errores 502/503 están en [REFERENCIA.md](./docs/REFERENCIA.md).

## 4. Comprobar RabbitMQ y la recepción durable

Desde `summaries-service/`:

```powershell
docker compose exec -T summaries-rabbitmq rabbitmqctl list_queues name messages consumers
Invoke-RestMethod "$base/api/v1/events" | ConvertTo-Json -Depth 8
```

Esperado: las colas `summaries_for_chat_messages_queue`, `summaries_for_summary_requested_queue` y `summaries_for_channel_entered_queue` tienen un consumidor cada una. `summaries_errors_queue` no tiene consumidor automático. Que messages=0 es normal: puede que los eventos ya se hayan consumido.

Para confirmar la recepción de las solicitudes en la base **propia** del servicio:

```powershell
docker compose exec -T summaries-mongodb mongosh summaries --quiet --eval 'db.summaries.find({}, {_id:0, id:1, messageState:1, _request_delivery:1}).sort({created_at:-1}).limit(5).toArray()'
```

Tras unos instantes, published=true y received=true indican publicación confirmada y recepción guardada. Después el worker lo toma y `messageState` pasa a `completada`. Si cambiaste MONGODB_DATABASE, reemplaza `summaries` en el comando. Esta inspección es solo de summaries-mongodb; no requiere entrar a bases del núcleo.

`GET /api/v1/events` lee los eventos desde Mongo (también `summary.generated` / `summary.failed` al terminar), así que sobreviven a reiniciar la aplicación.

## 5. Probar el resumen automático y los eventos de resultado

Para que dispare con pocos mensajes, baja el umbral al levantar el servicio (por defecto es 50): crea un archivo `.env` con `MESSAGE_THRESHOLD=3` y ejecuta `docker compose up -d`. Publica al menos 4 mensajes en el canal y simula la entrada de un usuario (lo que haría el Gateway):

```powershell
docker compose exec -T summaries python -m app.tools.publish_channel_entered $canalId $usuarioId entrada-1
docker compose exec -T summaries python -m app.tools.publish_channel_entered $canalId $usuarioId entrada-1
Start-Sleep 5
Invoke-RestMethod "$base/api/v1/channels/$canalId/summaries" | ConvertTo-Json -Depth 6
Invoke-RestMethod "$base/api/v1/events" | ConvertTo-Json -Depth 8
```

Esperado: **una sola** solicitud aunque se repita `entrada-1`, que termina `completada`; en los eventos aparecen `summary.requested` (con `trigger: automatic`) y `summary.generated`. Una segunda entrada sin mensajes nuevos no crea nada. Vuelve a borrar `.env` o `MESSAGE_THRESHOLD` cuando termines.

## 6. Logs, reinicio y apagado

```powershell
docker compose logs --tail 100 summaries
docker compose logs --tail 100 summaries-rabbitmq
docker compose restart summaries
```

Para seguir logs en vivo: `docker compose logs -f summaries`; Ctrl+C deja de seguirlos sin apagar los contenedores.

Para detener este servicio conservando sus volúmenes:

```powershell
docker compose down
```

Mongo conserva summaries-data y RabbitMQ summaries-rabbitmq-data. Vuelve a iniciar con `docker compose up -d --build --wait`. Para detener también el chat, usa `docker compose -p demo -f $composeChat stop`.

## 7. Problemas frecuentes

| Síntoma | Qué revisar |
| --- | --- |
| No existe la red microsvcs | Levantar primero el chat base con su compose |
| Puertos 8001–8004 ocupados | `docker ps`; verificar si ya está iniciado el mismo proyecto antes de levantar otro |
| GET /health responde ok pero no llega requested | Consumidores, logs RabbitMQ y flags published/received en Mongo |
| No llegan eventos de messages | Reaplicar el override RABBITMQ_URL y confirmar que usa el mismo broker |
| 503 en POST/listado/latest | Salud y conectividad de users/channels o Mongo; mirar logs de summaries |
| 502 del núcleo o error al crear datos en sus APIs | Revisar su Swagger y sus logs; validar que el núcleo responde antes de probar summaries |
| El resumen queda `pendiente` | RabbitMQ caído o `messages`/el servicio sin conexión al broker: revisar `published`/`received` en Mongo y los logs; al volver el broker se completa solo |
| El resumen termina `fallida` | Leer `error`: ventana vacía, cuota del LLM agotada, credencial inválida, etc. |
| Los `POST` de users/channels/messages del demo devuelven 500 | Es un bug del demo del curso (el dato sí se guarda); obtén los IDs con los GET. Ver README |
| El código de pruebas es distinto de 0 | Revisar la salida de pytest; no tomar el resultado del comando de limpieza como resultado de las pruebas |

Guía principal: [README.md](./README.md). Decisiones: [CLAUDE.md](./CLAUDE.md). Conceptos y contratos: [REFERENCIA.md](./docs/REFERENCIA.md).
