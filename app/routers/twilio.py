from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from loguru import logger
from pipecat.runner.types import WebSocketRunnerArguments

from app.services.bot import bot


router = APIRouter(prefix="/twilio", tags=["twilio"])


@router.post("/inbound")
async def inbound_call(request: Request) -> Response:
    """
    Twilio calls this webhook when a call comes in.
    Returns TwiML that tells Twilio to open a WebSocket stream to /twilio/ws.
    """
    host = request.headers.get("host")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/twilio/ws" />
    </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("Twilio WebSocket connected")

    try:
        runner_args = WebSocketRunnerArguments(websocket=websocket)
        runner_args.handle_sigint = False
        runner_args.pipeline_idle_timeout_secs = 30
        await bot(runner_args)

    except WebSocketDisconnect:
        logger.info("Twilio WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")