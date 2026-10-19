"""Endpoints REST de resúmenes (asíncrono con polling)."""
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from ..api_models import ErrorResponse
from ..models import Summary, SummaryAccepted, SummaryRequest
from ..repository import RepositoryDependency
from ..services import SummaryServiceDependency

router = APIRouter(prefix="/api/v1", tags=["summaries"])

CHANNEL_ID = Annotated[str, Path(description="ID del canal (uuid del servicio `channels`).",
                                 examples=["84e015c1-ed09-470c-b584-6f6210ea219c"])]
SUMMARY_ID = Annotated[str, Path(description="ID devuelto al solicitar el resumen.",
                                 examples=["08ac19f0-b81f-413c-be12-416d99f2672d"])]


def errors(*codes: int) -> dict:
    """Respuestas de error documentadas, con el formato uniforme `{"detail": ...}`."""
    catalog = {
        404: ("No existe el canal o el resumen.", "Channel not found"),
        422: ("Datos inválidos o usuario inexistente. Los errores de validación traen una lista en `detail`.",
              "User does not exist"),
        502: ("Un servicio del núcleo respondió algo inválido.", "Dependent service failed"),
        503: ("Un servicio del núcleo o el almacenamiento no está disponible.", "Dependent service unavailable"),
    }
    return {code: {"model": ErrorResponse, "description": catalog[code][0],
                   "content": {"application/json": {"example": {"detail": catalog[code][1]}}}} for code in codes}


@router.post(
    "/channels/{channel_id}/summaries",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SummaryAccepted,
    operation_id="requestSummary",
    summary="Solicitar un resumen",
    description=(
        "Valida el canal (y el usuario, si se indica), guarda la solicitud junto con el evento `summary.requested` "
        "y responde **202** sin esperar al LLM. Consulta el resultado con "
        "`GET /api/v1/summaries/{summary_id}` hasta que `messageState` sea `completada` o `fallida`.\n\n"
        "- **Sin cuerpo**: resume los mensajes nuevos desde el último resumen del canal.\n"
        "- **Con `timeInterval`**: resume solo ese rango `[start, end)` y no avanza el cursor del canal."),
    response_description="Solicitud aceptada; el resumen se genera en segundo plano.",
    responses={202: {"description": "Solicitud aceptada.", "headers": {"Location": {
        "description": "Ruta para consultar el resumen.", "schema": {"type": "string"},
        "example": "/api/v1/summaries/08ac19f0-b81f-413c-be12-416d99f2672d"}}}, **errors(404, 422, 502, 503)})
def request_summary(channel_id: CHANNEL_ID, response: Response, service: SummaryServiceDependency,
                    body: SummaryRequest | None = None):
    summary = service.request_summary(channel_id, body)
    response.headers["Location"] = f"/api/v1/summaries/{summary.id}"
    return SummaryAccepted(summary_id=summary.id)


@router.get(
    "/channels/{channel_id}/summaries/latest",
    response_model=Summary,
    operation_id="getLatestSummary",
    summary="Último resumen completado de un canal",
    description="Devuelve el resumen `completada` más reciente. Responde 404 si el canal no existe o aún no tiene ninguno.",
    responses=errors(404, 502, 503))
def latest_summary(channel_id: CHANNEL_ID, service: SummaryServiceDependency):
    return service.latest_summary(channel_id)


@router.get(
    "/channels/{channel_id}/summaries",
    response_model=list[Summary],
    operation_id="listSummaries",
    summary="Listar los resúmenes de un canal",
    description="Todos los estados, del más reciente al más antiguo, paginados con `limit` y `offset`.",
    responses=errors(404, 422, 502, 503))
def list_summaries(channel_id: CHANNEL_ID, service: SummaryServiceDependency,
                   limit: Annotated[int, Query(ge=1, le=1000, description="Máximo de resultados.")] = 100,
                   offset: Annotated[int, Query(ge=0, description="Cuántos saltar.")] = 0):
    return service.list_summaries(channel_id, limit=limit, offset=offset)


@router.get(
    "/summaries/{summary_id}",
    response_model=Summary,
    operation_id="getSummary",
    summary="Estado y contenido de un resumen (polling)",
    description=(
        "Lee solo la base propia (no llama al núcleo). `messageState` recorre `pendiente` → `procesando` → "
        "`completada` (con `content` y `structured`) o `fallida` (con `error`)."),
    responses=errors(404, 503))
def get_summary(summary_id: SUMMARY_ID, repository: RepositoryDependency):
    return repository.get_summary(summary_id)
