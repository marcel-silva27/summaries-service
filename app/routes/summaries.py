"""Endpoints REST de resúmenes (asíncrono con polling)."""
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from ..models import Summary, SummaryAccepted, SummaryRequest
from ..repository import RepositoryDependency
from ..services import SummaryServiceDependency

router = APIRouter(prefix="/api/v1", tags=["summaries"])

_ERRORS = {code: {"description": description} for code, description in (
    (404, "Channel or summary not found"), (422, "Invalid request or user does not exist"),
    (502, "Dependent service failed"), (503, "Dependency or storage unavailable"))}


@router.post("/channels/{channel_id}/summaries",
             status_code=status.HTTP_202_ACCEPTED,
             response_model=SummaryAccepted,
             responses=_ERRORS,
             summary="Solicitar un resumen (responde 202 + summary_id)")
def request_summary(channel_id: str, response: Response, service: SummaryServiceDependency,
                    body: SummaryRequest | None = None):
    summary = service.request_summary(channel_id, body)
    response.headers["Location"] = f"/api/v1/summaries/{summary.id}"
    return SummaryAccepted(summary_id=summary.id)


# `latest` debe declararse antes que rutas con parámetros del mismo prefijo.
@router.get("/channels/{channel_id}/summaries/latest",
            response_model=Summary,
            responses=_ERRORS,
            summary="Último resumen disponible del canal")
def latest_summary(channel_id: str, service: SummaryServiceDependency):
    return service.latest_summary(channel_id)


@router.get("/channels/{channel_id}/summaries",
            response_model=list[Summary],
            responses=_ERRORS,
            summary="Listar resúmenes de un canal")
def list_summaries(channel_id: str, service: SummaryServiceDependency,
                   limit: Annotated[int, Query(ge=1, le=1000)] = 100,
                   offset: Annotated[int, Query(ge=0)] = 0):
    return service.list_summaries(channel_id, limit=limit, offset=offset)


@router.get("/summaries/{summary_id}",
            response_model=Summary,
            responses={404: _ERRORS[404], 503: _ERRORS[503]},
            summary="Estado y contenido de un resumen (polling)")
def get_summary(summary_id: str, repository: RepositoryDependency):
    return repository.get_summary(summary_id)
