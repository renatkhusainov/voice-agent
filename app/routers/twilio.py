from xml.sax.saxutils import quoteattr

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, Depends
from fastapi.responses import Response
from loguru import logger
from pipecat.runner.types import WebSocketRunnerArguments

from app.services.bot import bot

from sqlalchemy import select
from app.db import get_db
from app.models.models import Practice
from sqlalchemy.orm import Session

router = APIRouter(prefix="/twilio", tags=["twilio"])


@router.post("/inbound", status_code=201)
async def inbound_call(request: Request, db: Session = Depends(get_db)) -> Response:

    form = await request.form()
    host = request.headers.get("host")
    call_from = form.get("From")
    call_to = form.get("To")
    call_sid = form.get("CallSid")

    # Numbers are identifiers (the caller's especially): log the sid, never them.
    practice = db.scalars(select(Practice).where(Practice.phone == call_to)).first()

    if practice is None:
        logger.warning("Inbound call sid={} to a number no practice owns", call_sid)
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>This number is not configured.</Say><Hangup/>
</Response>"""
    else:
        logger.info("Inbound call sid={} routed to practice={}", call_sid, practice.id)
        # Twilio sends these back in the stream's "start" message; Pipecat exposes
        # them as runner_args.call_data.body (from/to also as call_data.from_number/to_number)
        stream_params = {
            "practice_id": practice.id,
            "from_number": call_from or "",
            "to_number": call_to or "",
        }
        params_xml = "\n".join(
            f"            <Parameter name={quoteattr(name)} value={quoteattr(str(value))} />"
            for name, value in stream_params.items()
        )
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/twilio/ws">
{params_xml}
        </Stream>
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
