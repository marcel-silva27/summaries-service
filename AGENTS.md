# AGENTS.md — reglas rápidas para agentes

El contexto completo del proyecto está en [`CLAUDE.md`](./CLAUDE.md); el contrato técnico, en [`docs/REFERENCIA.md`](./docs/REFERENCIA.md). Esto es solo lo imprescindible.

## Comandos

```bash
# Pruebas (única forma soportada; levanta su propio Mongo y RabbitMQ)
docker compose -p summaries-tests -f compose.test.yaml up --build --abort-on-container-exit --exit-code-from tests --attach tests
docker compose -p summaries-tests -f compose.test.yaml down -v

# Entorno completo: ver README ("Cómo correrlo"). Requiere el chat base corriendo (crea la red `microsvcs`).
docker compose up -d --build --wait

# Regenerar los esquemas JSON de los eventos tras cambiar app/event_schemas.py
python -m app.tools.export_event_schemas
```

## Reglas

- **Nunca** leer ni escribir las bases de `users`, `channels` o `messages`, ni modificar archivos del repositorio del curso.
- **Nunca** poner credenciales en archivos versionados. `LLM_API_KEY` va solo en `.env` (ignorado por git), no en `.env.example`.
- **Nunca** llamar a la API real de Gemini desde pruebas automáticas (gasta cuota y envía datos a Google). Las pruebas usan el proveedor `mock` (lo fija `tests/conftest.py`) o `httpx.MockTransport`; el valor por defecto en ejecución es `gemini` y exige la key.
- No hacer merge a `main` ni subir cambios sin que se pida. Trabajar en ramas `feature/*`.
- Los datos de prueba que se creen en el demo del curso (usuarios, canales, mensajes) deben borrarse al terminar; no borrar lo que ya existía.
- Toda escritura que cambie estado y emita un evento debe hacerlo en **una sola operación** sobre el documento (outbox). No publicar al broker desde la ruta HTTP.
- Las respuestas públicas no exponen campos internos (`_...`) ni detalles de excepciones; los errores usan `{"detail": ...}`.
- Cambiar un modelo de evento exige regenerar `docs/eventos/` (hay una prueba que lo verifica).

## Convenciones

- Python 3.12, FastAPI, PyMongo síncrono, `httpx`, `pika`. IDs públicos: `uuid4` en el campo `id` (nunca el `_id` de Mongo).
- Fechas en UTC con zona y precisión de milisegundos; ventanas `[start, end)`.
- Rutas bajo `/api/v1`. Comentarios y documentación en español.
- Pydantic para toda forma de datos pública (respuestas, eventos, salida del LLM).
