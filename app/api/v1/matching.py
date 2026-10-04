from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.api.dependencies import get_actor, get_service
from app.schemas.matching import Connection, Decision, MatchingState, Message, MessageCreate, RequestCreate

router = APIRouter(prefix="/api/v1/matching", tags=["Matching"])


@router.get("/health/ready")
def ready(service=Depends(get_service)):
    try:
        service.repository.ready()
    except Exception as error:
        raise HTTPException(503, "Matching no está disponible.") from error
    return {"status": "ready", "service": "ms_matching"}


@router.get("/state", response_model=MatchingState)
def state(actor=Depends(get_actor), service=Depends(get_service)):
    return service.state(actor)


@router.post("/requests", response_model=Connection)
def request(payload: RequestCreate, actor=Depends(get_actor), service=Depends(get_service)):
    return service.request(actor, payload.recipient_id)


@router.post("/requests/{request_id}/decision", response_model=Connection)
def decision(request_id: UUID, payload: Decision, actor=Depends(get_actor), service=Depends(get_service)):
    return service.decide(actor, request_id, payload.action)


@router.delete("/requests/{request_id}", status_code=204)
def cancel(request_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    service.cancel(actor, request_id)
    return Response(status_code=204)


@router.get("/matches/{match_id}", response_model=Connection)
def match(match_id: UUID, actor=Depends(get_actor), service=Depends(get_service)):
    return service.match(actor, match_id)


@router.get("/matches/{match_id}/messages", response_model=list[Message])
def messages(match_id: UUID, after_id: int | None = Query(None, ge=0, le=9223372036854775807),
             before_id: int | None = Query(None, ge=1, le=9223372036854775807), limit: int = Query(50, ge=1, le=100),
             actor=Depends(get_actor), service=Depends(get_service)):
    return service.messages(actor, match_id, after_id, before_id, limit)


@router.post("/matches/{match_id}/messages", response_model=Message)
def send(match_id: UUID, payload: MessageCreate, actor=Depends(get_actor), service=Depends(get_service)):
    return service.send(actor, match_id, payload)
