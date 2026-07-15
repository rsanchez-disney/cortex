"""Order domain models — Pydantic schemas for request/response validation."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CreateOrderRequest(BaseModel):
    product_id: str
    quantity: int = Field(ge=1)
    customer_id: str
    shipping_address: str
    notes: Optional[str] = None


class UpdateOrderRequest(BaseModel):
    quantity: Optional[int] = Field(default=None, ge=1)
    shipping_address: Optional[str] = None
    notes: Optional[str] = None


class OrderItem(BaseModel):
    product_id: str
    product_name: str
    quantity: int
    unit_price: float


class OrderResponse(BaseModel):
    id: str
    product_id: str
    quantity: int
    customer_id: str
    shipping_address: str
    status: str
    created_at: datetime
    updated_at: Optional[datetime] = None


class OrderListResponse(BaseModel):
    orders: list[OrderResponse]
    total: int
    page: int = 1
    page_size: int = 100
