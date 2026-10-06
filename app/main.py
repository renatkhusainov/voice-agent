from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import settings
from app.fhir.client import FhirError
from app.mcp_server import build_http_app, mcp
from app.phi.log_scrub import secrets_from_environ, setup_logging
from app.routers import agent, rag, twilio
from app.routers.practice import router_practices, router_calls

# PHI and secrets never reach a log: see app/phi/log_scrub.py. Registered secrets
# are masked by exact value wherever they turn up (an error message, a URL).
setup_logging(secrets=[
    settings.anthropic_api_key,
    settings.deepgram_api_key,
    settings.twilio_auth_token,
    settings.twilio_account_sid,
    *secrets_from_environ(),
])

class _McpHttp:
    """ASGI stand-in for the MCP streamable-HTTP app. The MCP SDK's session
    manager can only run once per instance, and FastAPI doesn't run a mounted
    app's lifespan, so each lifespan below builds a fresh MCP app, starts its
    session manager and plugs it in here."""

    def __init__(self):
        self.app = None

    async def __call__(self, scope, receive, send):
        if self.app is None:
            raise RuntimeError("MCP transport used outside the app's lifespan")
        await self.app(scope, receive, send)


mcp_http = _McpHttp()


@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp_http.app = build_http_app()
    async with mcp.session_manager.run():
        yield
    mcp_http.app = None


app = FastAPI(lifespan=lifespan)

app.include_router(router_practices)
app.include_router(router_calls)
app.include_router(twilio.router)
app.include_router(agent.router)
app.include_router(rag.router)

@app.exception_handler(FhirError)
async def fhir_error(request: Request, exc: FhirError) -> JSONResponse:
    """A FHIR call failed underneath one of our endpoints: answer with the
    status it maps to (app/fhir/client.py). The body names HAPI's codes but
    never its diagnostics text, which can echo patient data from the request."""
    logger.warning("FHIR error on {} {}: {} status={} codes={}",
                   request.method, request.url.path, type(exc).__name__, exc.status, exc.codes)
    return JSONResponse(
        status_code=exc.api_status,
        content={"detail": type(exc).__name__, "fhir_status": exc.status, "fhir_codes": exc.codes},
    )


@app.get("/health")
def health():
    return {"status":"ok"}


# MCP over streamable HTTP at /mcp (app/mcp_server.py, docs/mcp.md). Mounted
# last and at the root: only paths no route above matched reach it, and the
# MCP app serves exactly one, /mcp, so it needs no trailing-slash redirect
# (which POST-based MCP clients don't reliably follow).
app.mount("/", mcp_http)
