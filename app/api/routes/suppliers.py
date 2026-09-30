from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_operator
from app.domain.models import Supplier
from app.infrastructure.db.session import get_session

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])
OperatorAccess = Annotated[None, Depends(require_operator)]
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]


class SupplierCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_name: str = Field(min_length=1, max_length=200)
    contact_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    whatsapp_number: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=320)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=100)
    market: str | None = Field(default=None, max_length=100)
    supplier_type: str | None = Field(default=None, max_length=100)
    gst_number: str | None = Field(default=None, max_length=32)


class SupplierRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    business_name: str
    contact_name: str | None
    phone: str | None
    whatsapp_number: str | None
    email: str | None
    city: str
    state: str
    market: str | None
    supplier_type: str | None
    gst_number: str | None
    is_verified: bool
    is_active: bool
    response_rate: float | None
    created_at: datetime
    updated_at: datetime


@router.post("", response_model=SupplierRead, status_code=status.HTTP_201_CREATED)
async def create_supplier(
    payload: SupplierCreate,
    session: DatabaseSession,
    _operator: OperatorAccess,
) -> Supplier:
    supplier = Supplier(**payload.model_dump())
    session.add(supplier)
    await session.commit()
    await session.refresh(supplier)
    return supplier


@router.get("", response_model=list[SupplierRead])
async def list_suppliers(
    session: DatabaseSession,
    _operator: OperatorAccess,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Supplier]:
    result = await session.scalars(
        select(Supplier).order_by(Supplier.business_name, Supplier.id).limit(limit).offset(offset)
    )
    return list(result)


@router.get("/{supplier_id}", response_model=SupplierRead)
async def get_supplier(
    supplier_id: UUID,
    session: DatabaseSession,
    _operator: OperatorAccess,
) -> Supplier:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")
    return supplier