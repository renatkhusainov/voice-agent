from fastapi import FastAPI
from app.routers.practice import router_practices, router_calls

app = FastAPI()

app.include_router(router_practices)
app.include_router(router_calls)

@app.get("/health")
def health():
    return {"status":"ok"}

