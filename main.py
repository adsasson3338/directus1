"""
main.py — Entry point. Creates FastAPI app, includes discovery and ingestion routers.
"""
from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncio

from shared import cleanup_stale_jobs
from discovery import router as discovery_router
from ingestion import router as ingestion_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(cleanup_stale_jobs())
    yield


app = FastAPI(title="Sales Data Pipeline", version="1.0.0", lifespan=lifespan)

app.include_router(discovery_router)
app.include_router(ingestion_router)


@app.get("/")
def root():
    return {"status": "ok", "service": "Sales Data Pipeline", "version": "1.0.0"}
