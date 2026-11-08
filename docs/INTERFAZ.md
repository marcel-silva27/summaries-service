# Entrega de interfaz y API Gateway

La interfaz del chat usa las APIs de resúmenes, búsqueda, medios y encuestas a través de un único origen. Los formularios crean usuarios/canales, envían mensajes, solicitan resúmenes, suben y borran archivos y crean/votan/cierran encuestas. Los resultados y la búsqueda se consultan desde el gateway. `compose.peers.yaml` conecta las versiones públicas revisadas a `microsvcs`, sin editar sus fuentes.

## Demostración

[Ver video MP4](evidencia/demo-interfaz.mp4) · [Captura de escritorio](evidencia/interfaz.png) · [Captura móvil](evidencia/interfaz-movil.png).

La verificación graba el navegador usando los servicios reales de los equipos, MongoDB, RabbitMQ, PostgreSQL y Elasticsearch en un proyecto Docker descartable. **Gemini está simulado**: nunca se llama a su API desde la automatización. El proveedor normal sigue siendo Gemini y requiere la key local.

El arranque integrado declara `hilo.polls.events` como cola durable de auditoría de `polls.events` (`poll.#`). Así Polls puede confirmar sus eventos aunque ningún otro equipo esté consumiéndolos todavía; la cola no interviene en las respuestas HTTP de la interfaz.

Para reproducirla, prepara los equipos con `python tools/prepare_peers.py`. Define `POLLS_DB_PASSWORD` en tu `.env` y ten FFmpeg disponible. Instala las dependencias opcionales:

```bash
python -m pip install -r requirements-browser.txt
python -m playwright install ffmpeg
```

El script usa Edge en Windows; en otros sistemas instala Chromium con `python -m playwright install chromium`.

En PowerShell, elige una red y puertos que no estén siendo usados:

```powershell
$env:CHAT_NETWORK = "hilo-demo-isolated"
$env:GATEWAY_PORT = "18080"
$env:SUMMARIES_PORT = "18004"
docker compose -p hilo-demo -f docker-compose.yaml -f compose.peers.yaml -f compose.demo-test.yaml up -d --build --wait
python tools/record_demo.py --isolated
docker compose -p hilo-demo -f docker-compose.yaml -f compose.peers.yaml -f compose.demo-test.yaml down -v
```

En Linux/macOS usa las mismas variables con `export`. El script crea usuarios, un canal, mensajes, un adjunto y una encuesta ficticios. El `down -v` final elimina exclusivamente los contenedores y volúmenes de ese proyecto, incluidos todos esos datos. No uses el script contra el entorno de un equipo con datos existentes.

La grabación comprueba envío de mensajes, resumen hasta estado terminal, búsqueda con filtros, descarga idéntica al original, borrado por propietario, previews de enlaces, voto con otro usuario, cierre y anuncio de resultados. También comprueba la vista móvil, ausencia de errores JavaScript y que las peticiones de la web no accedan directamente a los servicios.

## Límites conocidos

- Identidad declarada sin autenticación, como los servicios del curso; `X-User-Id` tampoco autentica.
- Búsqueda y previews son eventualmente consistentes. La web maneja el `202` del índice en preparación.
- Polls no ofrece listado global; sus IDs se descubren por anuncios y se recuerdan en el navegador.
- No se ofrecen edición/borrado de mensajes: el historial de resúmenes conserva mensajes inmutables. Los cambios de otros clientes requieren revisar esa compatibilidad.
- El evento de entrada a canal sigue requiriendo un productor externo; la web utiliza el flujo de resúmenes bajo demanda.
- El Compose integrado usa un exchange `chat` durable y admite los metadatos nuevos de los mensajes del equipo. El demo original requiere `CHAT_EXCHANGE_DURABLE=false`.

Las pruebas completas se ejecutan con el comando Docker indicado en el README. No se requiere conectarse a APIs externas ni a Gemini para esa suite.
