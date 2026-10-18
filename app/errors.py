"""Errores explícitos del servicio, con el formato {"detail": ...} del curso."""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError


class ServiceError(Exception):
    status_code = 500
    detail = "Internal error"

    def __init__(self, detail: str | None = None):
        self.detail = detail or self.detail


class ChannelNotFound(ServiceError):
    status_code = 404
    detail = "Channel not found"


class UserNotFound(ServiceError):
    status_code = 422
    detail = "User does not exist"


class SummaryNotFound(ServiceError):
    status_code = 404
    detail = "Summary not found"


class DependencyFailed(ServiceError):
    status_code = 502
    detail = "Dependent service failed"


class DependencyUnavailable(ServiceError):
    status_code = 503
    detail = "Dependent service unavailable"


class PersistenceConflict(ServiceError):
    status_code = 409
    detail = "Persistent data conflict"


class SummaryStateConflict(PersistenceConflict):
    detail = "Summary state does not allow this operation"


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(PyMongoError)
    async def mongo_error_handler(_: Request, exc: PyMongoError):
        return JSONResponse(status_code=503, content={"detail": "Summary storage unavailable"})
