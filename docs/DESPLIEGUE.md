# Despliegue en Kubernetes

El servicio corre en el cluster común del curso (DigitalOcean), en el namespace `grupo-resumenes`, contra el chat base desplegado en `servicios-base`. El core no se redesplegó ni se modificó: solo se usan sus APIs.

URL pública: `http://resumenes.134.199.176.3.nip.io` (`/docs` para la documentación interactiva).

## Qué se despliega

Manifiestos en [`k8s/`](../k8s), aplicados con `kubectl apply -k k8s/`.

| Recurso | Detalle |
| --- | --- |
| `Deployment/summaries` | Imagen de GHCR fijada al SHA del commit; `startupProbe`, `readinessProbe` y `livenessProbe` en `/health`; usuario no root; `requests` 100m/128Mi, `limits` 250m/256Mi |
| `HorizontalPodAutoscaler/summaries` | CPU al 60 %, entre 2 y 4 réplicas |
| `StatefulSet/summaries-mongodb` | Mongo 7 con el único PVC (1Gi) que permite la cuota |
| `Deployment/summaries-rabbitmq` | Broker propio, sin PVC: si se reinicia, los eventos siguen en la outbox de Mongo y se republican |
| `Service` + `Ingress` | ClusterIP e Ingress Traefik con host `nip.io` |
| `ConfigMap/summaries-config` | Equivale a `.env.example`; el núcleo se alcanza en `http://<servicio>.servicios-base:8000` |
| `Secret/summaries-llm` | `LLM_API_KEY`, creado a mano en el cluster; no está en el repositorio ni en CI |

Límites que condicionan el diseño (cuota del namespace): 1 CPU y 1Gi de `requests`, 2 CPU y 2Gi de `limits`, 10 pods y un PVC de 1Gi. Por eso el máximo es de 4 réplicas y el despliegue usa `maxSurge: 0`.

`/health` no consulta Mongo ni RabbitMQ (el código del servicio no se modificó para el despliegue): si Mongo no está disponible al arrancar, el proceso termina y Kubernetes lo reinicia hasta que lo esté.

## Integración continua

[`.github/workflows/deploy.yaml`](../.github/workflows/deploy.yaml) (GitHub Actions):

1. En cada PR y push: las pruebas automáticas (`compose.test.yaml`).
2. En push a `main`: construye la imagen y la publica en `ghcr.io/marcel-silva27/summaries-service` con el SHA del commit.
3. Fija ese SHA en el manifiesto, aplica `k8s/` y espera el rollout.

Requiere el secret de GitHub `KUBECONFIG_B64` (`base64 -w0 <kubeconfig>`).

## Evidencia: flujo integrado con el chat base

Fecha: 2026-10-30. Datos ficticios con prefijo `resumenes-test`.

1. En el core desplegado (desde un pod del namespace, único con acceso de red): 3 usuarios, el canal `resumenes-test-planificacion` (`0ac7c358-854d-4ceb-911e-962c2f09a8c0`) y 12 mensajes de una conversación sobre el lanzamiento de una versión. Las 16 escrituras respondieron `201`.
2. Por la URL pública:
   ```text
   POST /api/v1/channels/0ac7c358-…/summaries  {"user_id": "aadbfe21-…"}
   → 202 Accepted, Location: /api/v1/summaries/5d51a0ee-d939-4c57-b22d-51f12c37c9ae
   ```
   El `202` implica que el servicio validó el canal y el usuario contra `channels` y `users` del cluster.
3. Resultado con Gemini (`gemini-3.5-flash-lite`): `completada`. El `digest` registra los 12 mensajes leídos de `messages`, sin omitidos. Extracto de `structured`:
   - **Decisiones:** lanzamiento el viernes 13 de noviembre a las 10:00; la integración de pagos queda para la 2.1; si las pruebas fallan, se pospone al lunes 16.
   - **Eventos:** lanzamiento (viernes 13, 10:00); reunión de seguimiento (jueves 12, 16:00).
   - **Tareas:** notas de la versión → `resumenes-test-bruno`; pruebas de regresión → `resumenes-test-carla`; coordinar con soporte → `resumenes-test-ana`.
4. `GET …/summaries/latest` → `200`; el listado del canal muestra el resumen; `GET /api/v1/events` contiene `summary.requested` y `summary.generated`.

**Resiliencia observada:** durante la prueba, RabbitMQ estaba en `CrashLoopBackOff` (probes mal configuradas, ver abajo). La solicitud quedó `pendiente` en la outbox de Mongo y, al corregirse el broker, se completó sola sin reenviarla (23:13 → 23:45 UTC).

Registros en [`evidencia/`](./evidencia):

| Archivo | Contenido |
| --- | --- |
| [`01-sembrar-core.py`](./evidencia/01-sembrar-core.py) / [`.txt`](./evidencia/01-sembrar-core.txt) | Script que crea los datos en el core (cada `POST` una sola vez) y su salida con los IDs |
| [`02-solicitud-y-polling.txt`](./evidencia/02-solicitud-y-polling.txt) | `POST` con `202` y `Location`, y 30 consultas que quedan en `pendiente` mientras el broker estaba caído |
| [`03-resultado-del-flujo.txt`](./evidencia/03-resultado-del-flujo.txt) | El resumen `completada` completo, `latest`, el listado del canal y los eventos |

## Evidencia: autoscaling

Carga con [`k8s/loadtest/job.yaml`](../k8s/loadtest/job.yaml): un `Job` en el namespace que llama a `GET /api/v1/summaries/{id}` a través del Service interno durante 4 minutos con 40 conexiones. Ese endpoint solo lee la base propia, así que la prueba no genera tráfico al core ni a Gemini, no crea datos y no pasa por el Ingress compartido.

Resultado: 8329 solicitudes (~35 por segundo), todas `200`.

| Hora (UTC) | CPU / objetivo | Réplicas | |
| --- | --- | --- | --- |
| 00:04:19 | 6 % / 60 % | 2 | Reposo |
| 00:04:43 | 63 % | 2 | Empieza la carga |
| 00:05:19 | 77 % | **3** | Escala hacia arriba |
| 00:05:31 | 82 % | 3 | La nueva réplica toma carga (174m de CPU) |
| 00:05:43 – 00:07:59 | 51–63 % | 3 | Estable cerca del objetivo |
| 00:08:56 | 6 % | 3 | Terminó la carga |
| 00:09:44 | 6 % | **2** | Escala hacia abajo |

No llegó a 4 réplicas porque con 3 la CPU quedó en torno al objetivo.

Registros: [`04-carga.txt`](./evidencia/04-carga.txt) (salida del Job, solicitudes por segundo y códigos cada 15 s) y [`05-monitor-hpa.txt`](./evidencia/05-monitor-hpa.txt) (`kubectl get hpa`, `get pods` y `top pods` cada ~10 s, de 00:03 a 00:14 UTC).

Para repetirlo: `kubectl create -f k8s/loadtest/job.yaml` y, en paralelo, `kubectl get hpa summaries -w`.

## Defectos encontrados al desplegar

| Defecto | Corrección |
| --- | --- |
| RabbitMQ en `CrashLoopBackOff` (`.erlang.cookie: eacces`): la probe `rabbitmq-diagnostics` corría como root y, si se adelantaba al servidor, creaba el archivo con ese dueño | Probes TCP al puerto 5672 |
| Cada deploy reiniciaba los pods dos veces (aplicaba `:latest` y luego el SHA) | El SHA se fija en el manifiesto antes de aplicar |
| Al primer arranque, el servicio salió porque el DNS de Mongo aún no existía | Kubernetes lo reinició y arrancó; sin cambios |
