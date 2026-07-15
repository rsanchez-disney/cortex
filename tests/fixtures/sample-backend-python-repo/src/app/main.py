"""Order Service — FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import orders, health

app = FastAPI(
    title="Order Service",
    version="1.2.0",
    description="Order management microservice",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(orders.router, prefix="/api/v1", tags=["orders"])
app.include_router(health.router, tags=["health"])


@app.get("/")
async def root():
    """Root endpoint."""
    return {"service": "order-service", "status": "running"}
