"""Order API router — CRUD endpoints for order management."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import (
    CreateOrderRequest,
    OrderResponse,
    UpdateOrderRequest,
    OrderListResponse,
)
from app.database import get_db

router = APIRouter()


@router.post("/api/v1/orders", response_model=OrderResponse)
async def create_order(order: CreateOrderRequest, db: AsyncSession = Depends(get_db)):
    """Create a new order."""
    # Business logic here
    return {"id": "new-order-id", "status": "created"}


@router.get("/api/v1/orders/{order_id}", response_model=OrderResponse)
async def get_order(order_id: str, db: AsyncSession = Depends(get_db)):
    """Retrieve a specific order by ID."""
    return {"id": order_id, "status": "pending"}


@router.get("/api/v1/orders", response_model=OrderListResponse)
async def list_orders(skip: int = 0, limit: int = 100, db: AsyncSession = Depends(get_db)):
    """List all orders with pagination."""
    return {"orders": [], "total": 0}


@router.put("/api/v1/orders/{order_id}", response_model=OrderResponse)
async def update_order(order_id: str, order: UpdateOrderRequest, db: AsyncSession = Depends(get_db)):
    """Update an existing order."""
    return {"id": order_id, "status": "updated"}


@router.delete("/api/v1/orders/{order_id}")
async def delete_order(order_id: str, db: AsyncSession = Depends(get_db)):
    """Delete an order."""
    return {"message": "Order deleted"}
