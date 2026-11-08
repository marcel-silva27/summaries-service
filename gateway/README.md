# API Gateway e interfaz

FastAPI + Ariadne. Sirve la web de `web/` y agrega las APIs REST de usuarios, canales, mensajes, resúmenes, búsqueda, medios y encuestas. No almacena datos ni publica al broker desde HTTP.

Arranque y requisitos: [README principal](../README.md). Web en `/`; GraphQL y explorador en `/api/v1/graphql`; salud en `/health`.

## Contratos integrados

| Equipo | Repositorio y versión revisada | Funciones de la interfaz |
| --- | --- | --- |
| Búsqueda y núcleo | [Mochytk](https://github.com/Mochytk/INF326-Arquitectura-de-Software_Tareas/tree/54940ec524ffc6f7ed3a3d7f6cf0801dcf43aa06/Tarea_Unidad_5_Despliegue) | Usuarios, canales, mensajes y búsqueda por texto/autor/fechas/enlaces |
| Medios | [667r](https://github.com/667r/microservicios/tree/19f0ef79ecefbb6392b85be2571928e9dcedc0ea/03_Comunicaci%C3%B3n/demo/media) | Subida, galería, descarga, borrado, cuota y previews |
| Encuestas | [TomasCano-Usm](https://github.com/TomasCano-Usm/INF326-Microservicio-Polls/tree/76df544c42a6e1cc42fab490e1b200523faf0f10) | Crear, consultar, votar y cerrar |
| Resúmenes | Este repositorio | Solicitar, consultar estado, listar y leer el último |

El esquema completo está en [schema.graphql](schema.graphql). Ejemplo para otro frontend:

```graphql
mutation {
  requestSummary(channel_id: "CHANNEL_ID", input: {user_id: "USER_ID"}) {
    summary_id
    messageState
  }
}
```

Luego: `query { getSummary(id: "SUMMARY_ID") { messageState content error } }`. `input` acepta `timeInterval: {start: "...", end: "..."}` con zona horaria.

Envía `POST /api/v1/graphql` con JSON `{"query":"...","variables":{...}}`. Los errores incluyen `errors[].extensions = {detail, status, service}`. Los campos anulables permiten devolver resultados parciales si falla una dependencia; no se reenvían cuerpos de error internos. No hay reintentos automáticos de escrituras.

Search `202` se representa como `indexing: true` y `retry_after`. `createPoll` exige una `idempotency_key` estable al reintentar. Polls no tiene listado por canal: la web obtiene los IDs de sus anuncios `Poll ID:` y permite importarlos.

## Archivos

La transferencia binaria también pasa por el gateway, con `X-User-Id`:

| Método | Ruta del gateway |
| --- | --- |
| POST multipart, campo `file` | `/api/v1/media/channels/{channel_id}/attachments` |
| GET | `/api/v1/media/attachments/{attachment_id}/download` |
| DELETE | `/api/v1/media/attachments/{attachment_id}` |

Máximo 10 MiB por subida. Media valida formato, cuota e identidad; solo el uploader puede borrar. Las descargas conservan `Content-Disposition` y usan `nosniff`.

Configura las URLs sin `/api/v1` mediante las variables del README principal. `GATEWAY_TIMEOUT_SECONDS` (10) limita las llamadas. La web usa el mismo origen; un frontend externo puede agregarse a `GATEWAY_CORS_ORIGINS` (orígenes separados por comas).

Los demás equipos también pueden conectar su propio gateway directamente a las rutas REST de resúmenes en `http://summaries:8000/api/v1`. La solicitud devuelve `202` y `summary_id`; el resultado se consulta por polling. El gateway incluido no emite `chat.channel.entered`.
