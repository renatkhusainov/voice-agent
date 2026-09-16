from fastapi import FastAPI
from app.routers import twilio
from app.routers.practice import router_practices, router_calls

app = FastAPI()

app.include_router(router_practices)
app.include_router(router_calls)
app.include_router(twilio.router)

@app.get("/health")
def health():
    return {"status":"ok"}
