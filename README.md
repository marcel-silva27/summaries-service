# Servicio de Resúmenes

Capacidad vertical para la cátedra de Microservicios. Genera resúmenes automáticos (vía LLM) sobre las conversaciones del chat base del curso ([rmasad/microservicios](https://github.com/rmasad/microservicios)), sin acceder a sus bases de datos: toda interacción ocurre por sus APIs públicas o por eventos del broker.

Detalle de dominio, contratos y decisiones de arquitectura: ver [`CLAUDE.md`](./CLAUDE.md) y la [propuesta completa](https://claude.ai/artifact/BSeEEVB7yRPouL1wqsg497).

## Estructura

```
summaries-service/
├── CLAUDE.md                 # Contexto de dominio para Claude Code
├── README.md                 # Este archivo
├── docker-compose.yaml       # Se une a la red externa `microsvcs`
├── Dockerfile
├── requirements.txt
└── app/
    ├── main.py                # Entry point FastAPI
    ├── models.py               # Schemas: summary, digest, checkpoint, messageState
    ├── routes/
    │   └── summaries.py        # Endpoints REST (/api/v1/channels/{id}/summaries, /api/v1/summaries/{id})
    ├── events.py                # Consumidor (chat.message.created, summary.requested) y publisher (summary.generated/failed)
    └── clients/
        ├── users_client.py      # Llamadas a GET /api/v1/users/{id} del núcleo
        ├── channels_client.py   # Llamadas a GET /api/v1/channels/{id}
        ├── messages_client.py   # Llamadas a GET /api/v1/channels/{id}/messages
        └── llm_client.py        # Llamadas a POST <LLM_API>/generate
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

3. Documentación OpenAPI de este servicio: `http://localhost:<puerto>/docs`.

No se modifica nada dentro de `microservicios/`; ese repo queda solo como referencia y para levantar el núcleo.

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
| `USERS_URL` | Base URL del servicio `users` del núcleo |
| `CHANNELS_URL` | Base URL del servicio `channels` del núcleo |
| `MESSAGES_URL` | Base URL del servicio `messages` del núcleo |
| `LLM_API_URL` | Endpoint del proveedor de LLM |
| `BROKER_URL` | Conexión al broker de eventos |

## Estado

🚧 En desarrollo — entrega para la Unidad 4 del curso.
