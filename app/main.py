from fastapi import FastAPI

from app.config import settings
from app.phi.log_scrub import secrets_from_environ, setup_logging
from app.routers import agent, twilio
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

app = FastAPI()

app.include_router(router_practices)
app.include_router(router_calls)
app.include_router(twilio.router)
app.include_router(agent.router)

@app.get("/health")
def health():
    return {"status":"ok"}
