"""Endpoints REST de resúmenes (asíncrono con polling)."""
from fastapi import APIRouter, HTTPException, status

from ..models import Summary, SummaryAccepted, SummaryRequest

router = APIRouter(prefix="/api/v1", tags=["summaries"])

_NOT_IMPLEMENTED = HTTPException(status_code=501, detail="Not implemented")


@router.post("/channels/{channel_id}/summaries",
             status_code=status.HTTP_202_ACCEPTED,
             response_model=SummaryAccepted,
             summary="Solicitar un resumen (responde 202 + summary_id)")
def request_summary(channel_id: str, body: SummaryRequest | None = None):
    raise _NOT_IMPLEMENTED


# `latest` debe declararse antes que rutas con parámetros del mismo prefijo.
@router.get("/channels/{channel_id}/summaries/latest",
            response_model=Summary,
            summary="Último resumen disponible del canal")
def latest_summary(channel_id: str):
    raise _NOT_IMPLEMENTED


@router.get("/channels/{channel_id}/summaries",
            response_model=list[Summary],
            summary="Listar resúmenes de un canal")
def list_summaries(channel_id: str):
    raise _NOT_IMPLEMENTED


@router.get("/summaries/{summary_id}",
            response_model=Summary,
            summary="Estado y contenido de un resumen (polling)")
def get_summary(summary_id: str):
    raise _NOT_IMPLEMENTED
