from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_operator
from app.domain.catalogue_ingestion import CatalogueParseResult, parse_catalogue_spreadsheet
from app.domain.matching import MatchWeights, SupplierOffer, rank_supplier_offers
from app.domain.models import Catalogue, CatalogueItem, Supplier
from app.domain.requirements import (
    AttributeProvenance,
    AttributeSource,
    ProductCategory,
    Requirement,
)
from app.domain.statuses import CatalogueSourceType, CatalogueStatus
from app.infrastructure.db.session import get_session
from app.infrastructure.storage.local import LocalMediaStorage
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api/catalogues", tags=["catalogues"])
OperatorAccess = Annotated[None, Depends(require_operator)]
DatabaseSession = Annotated[AsyncSession, Depends(get_session)]
ApplicationSettings = Annotated[Settings, Depends(get_settings)]
UploadedFile = Annotated[UploadFile, File()]
MAX_CATALOGUE_UPLOAD_BYTES = 15 * 1024 * 1024
CATALOGUE_EXTENSIONS = {
    ".csv": CatalogueSourceType.CSV,
    ".xlsx": CatalogueSourceType.EXCEL,
    ".xlsm": CatalogueSourceType.EXCEL,
    ".pdf": CatalogueSourceType.PDF,
    ".jpg": CatalogueSourceType.IMAGE,
    ".jpeg": CatalogueSourceType.IMAGE,
    ".png": CatalogueSourceType.IMAGE,
    ".webp": CatalogueSourceType.IMAGE,
}


class CatalogueCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: UUID
    source_type: CatalogueSourceType
    source_url: str | None = None
    last_verified_at: datetime | None = None


class CatalogueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    supplier_id: UUID
    source_type: CatalogueSourceType
    source_url: str | None
    last_verified_at: datetime | None
    status: CatalogueStatus
    created_at: datetime
    updated_at: datetime


class CatalogueItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    category: ProductCategory
    subcategory: str | None = Field(default=None, max_length=100)
    fabric: str | None = Field(default=None, max_length=100)
    color: str | None = Field(default=None, max_length=100)
    style: str | None = Field(default=None, max_length=200)
    price: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    moq: int | None = Field(default=None, gt=0)
    stock_status: str | None = Field(default=None, max_length=50)
    available_quantity: int | None = Field(default=None, ge=0)
    image_url: str | None = None
    last_verified_at: datetime | None = None
    fact_provenance: dict[str, AttributeProvenance] = Field(default_factory=dict)


class CatalogueItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    catalogue_id: UUID
    supplier_id: UUID
    title: str
    description: str | None
    category: ProductCategory
    subcategory: str | None
    fabric: str | None
    color: str | None
    style: str | None
    price: Decimal | None
    currency: str | None
    moq: int | None
    stock_status: str | None
    available_quantity: int | None
    image_url: str | None
    fact_provenance: dict[str, Any]
    last_verified_at: datetime | None


class CatalogueSearchResult(BaseModel):
    catalogue_item_id: UUID
    supplier_id: UUID
    supplier_name: str
    title: str
    price: Decimal | None
    currency: str | None
    moq: int | None
    available_quantity: int | None
    last_verified_at: datetime | None
    catalogue_age_days: int | None
    match_score: float
    reasons: list[str]


class CatalogueUploadResult(BaseModel):
    catalogue_id: UUID
    stored_key: str
    total_rows: int
    items_created: int
    errors: list[dict[str, Any]]
    review_required: bool


class CatalogueVerification(BaseModel):
    verified: bool = True


@router.post("", response_model=CatalogueRead, status_code=status.HTTP_201_CREATED)
async def create_catalogue(
    payload: CatalogueCreate,
    session: DatabaseSession,
    _operator: OperatorAccess,
) -> Catalogue:
    supplier = await session.get(Supplier, payload.supplier_id)
    if supplier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")
    catalogue = Catalogue(**payload.model_dump())
    session.add(catalogue)
    await session.commit()
    await session.refresh(catalogue)
    return catalogue


@router.get("", response_model=list[CatalogueRead])
async def list_catalogues(
    session: DatabaseSession,
    _operator: OperatorAccess,
    supplier_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Catalogue]:
    statement = select(Catalogue).order_by(Catalogue.created_at.desc()).limit(limit).offset(offset)
    if supplier_id is not None:
        statement = statement.where(Catalogue.supplier_id == supplier_id)
    result = await session.scalars(statement)
    return list(result)


@router.post(
    "/{catalogue_id}/items",
    response_model=CatalogueItemRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_catalogue_item(
    catalogue_id: UUID,
    payload: CatalogueItemCreate,
    session: DatabaseSession,
    _operator: OperatorAccess,
) -> CatalogueItem:
    catalogue = await session.get(Catalogue, catalogue_id)
    if catalogue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalogue not found")

    item_data = payload.model_dump(exclude={"fact_provenance"})
    provenance = payload.fact_provenance.copy()
    source_id = str(catalogue.id)
    for field_name, value in item_data.items():
        if value is not None and field_name not in provenance:
            provenance[field_name] = AttributeProvenance(
                source=AttributeSource.SUPPLIER_CATALOGUE,
                source_id=source_id,
            )

    item = CatalogueItem(
        catalogue_id=catalogue.id,
        supplier_id=catalogue.supplier_id,
        **item_data,
        fact_provenance={
            field_name: fact.model_dump(mode="json")
            for field_name, fact in provenance.items()
        },
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


@router.post("/{catalogue_id}/upload", response_model=CatalogueUploadResult, status_code=202)
async def upload_catalogue_file(
    catalogue_id: UUID,
    session: DatabaseSession,
    _operator: OperatorAccess,
    settings: ApplicationSettings,
    file: UploadedFile,
) -> CatalogueUploadResult:
    catalogue = await session.get(Catalogue, catalogue_id)
    if catalogue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalogue not found")
    filename = Path(file.filename or "upload").name
    extension = Path(filename).suffix.lower()
    source_type = CATALOGUE_EXTENSIONS.get(extension)
    if source_type is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Upload CSV, XLSX, PDF, JPG, PNG, or WebP files",
        )
    content = await file.read(MAX_CATALOGUE_UPLOAD_BYTES + 1)
    if len(content) > MAX_CATALOGUE_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Catalogue upload exceeds 15 MB",
        )

    parsed: CatalogueParseResult | None = None
    if source_type in {CatalogueSourceType.CSV, CatalogueSourceType.EXCEL}:
        try:
            parsed = parse_catalogue_spreadsheet(filename, content)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(error),
            ) from error

    media_storage = LocalMediaStorage(settings.media_storage_dir)
    try:
        object_key = await media_storage.save(filename, file.content_type or "application/octet-stream", content)
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Catalogue storage is unavailable",
        ) from error

    catalogue.source_type = source_type
    catalogue.source_url = object_key
    catalogue.status = CatalogueStatus.NEEDS_REVIEW
    created_count = 0
    if parsed is not None:
        for row in parsed.rows:
            values = row.model_dump()
            provenance = {
                field_name: AttributeProvenance(
                    source=AttributeSource.SUPPLIER_CATALOGUE,
                    source_id=str(catalogue.id),
                ).model_dump(mode="json")
                for field_name, value in values.items()
                if value is not None
            }
            session.add(
                CatalogueItem(
                    catalogue_id=catalogue.id,
                    supplier_id=catalogue.supplier_id,
                    **values,
                    fact_provenance=provenance,
                )
            )
            created_count += 1
    await session.commit()
    return CatalogueUploadResult(
        catalogue_id=catalogue.id,
        stored_key=object_key,
        total_rows=parsed.total_rows if parsed is not None else 0,
        items_created=created_count,
        errors=[error.model_dump() for error in parsed.errors] if parsed is not None else [],
        review_required=True,
    )


@router.post("/{catalogue_id}/verify", response_model=CatalogueRead)
async def verify_catalogue(
    catalogue_id: UUID,
    verification: CatalogueVerification,
    session: DatabaseSession,
    _operator: OperatorAccess,
) -> Catalogue:
    catalogue = await session.get(Catalogue, catalogue_id)
    if catalogue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalogue not found")
    if not verification.verified:
        catalogue.status = CatalogueStatus.NEEDS_REVIEW
        await session.commit()
        await session.refresh(catalogue)
        return catalogue

    now = datetime.now(UTC)
    catalogue.status = CatalogueStatus.ACTIVE
    catalogue.last_verified_at = now
    items = await session.scalars(
        select(CatalogueItem).where(CatalogueItem.catalogue_id == catalogue.id)
    )
    for item in items:
        item.last_verified_at = now
    await session.commit()
    await session.refresh(catalogue)
    return catalogue


@router.get("/{catalogue_id}/items", response_model=list[CatalogueItemRead])
async def list_catalogue_items(
    catalogue_id: UUID,
    session: DatabaseSession,
    _operator: OperatorAccess,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CatalogueItem]:
    if await session.get(Catalogue, catalogue_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Catalogue not found")
    result = await session.scalars(
        select(CatalogueItem)
        .where(CatalogueItem.catalogue_id == catalogue_id)
        .order_by(CatalogueItem.title, CatalogueItem.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result)


@router.post("/search", response_model=list[CatalogueSearchResult])
async def search_catalogue(
    requirement: Requirement,
    session: DatabaseSession,
    _operator: OperatorAccess,
) -> list[CatalogueSearchResult]:
    statement = (
        select(CatalogueItem, Supplier, Catalogue)
        .join(Supplier, Supplier.id == CatalogueItem.supplier_id)
        .join(Catalogue, Catalogue.id == CatalogueItem.catalogue_id)
        .where(
            Supplier.is_active.is_(True),
            Catalogue.status == CatalogueStatus.ACTIVE,
        )
    )
    if requirement.category is not None:
        statement = statement.where(CatalogueItem.category == requirement.category)
    if requirement.fabric is not None:
        statement = statement.where(
            func.lower(CatalogueItem.fabric) == requirement.fabric.casefold()
        )
    if requirement.quantity is not None:
        statement = statement.where(
            or_(CatalogueItem.moq.is_(None), CatalogueItem.moq <= requirement.quantity)
        )

    rows = (await session.execute(statement)).all()
    items_by_id = {str(item.id): (item, supplier, catalogue) for item, supplier, catalogue in rows}
    offers = [
        SupplierOffer(
            supplier_id=str(supplier.id),
            catalogue_item_id=str(item.id),
            category=item.category,
            fabric=item.fabric,
            price=item.price,
            currency=item.currency,
            moq=item.moq,
            supplier_city=supplier.city,
            is_active=supplier.is_active,
            is_verified=supplier.is_verified,
            catalogue_age_days=_catalogue_age_days(
                item.last_verified_at or catalogue.last_verified_at
            ),
            response_rate=(
                float(supplier.response_rate) if supplier.response_rate is not None else None
            ),
        )
        for item, supplier, catalogue in rows
    ]
    ranked = rank_supplier_offers(requirement, offers, weights=MatchWeights())

    return [
        CatalogueSearchResult(
            catalogue_item_id=UUID(match.catalogue_item_id),
            supplier_id=UUID(match.supplier_id),
            supplier_name=items_by_id[match.catalogue_item_id][1].business_name,
            title=items_by_id[match.catalogue_item_id][0].title,
            price=items_by_id[match.catalogue_item_id][0].price,
            currency=items_by_id[match.catalogue_item_id][0].currency,
            moq=items_by_id[match.catalogue_item_id][0].moq,
            available_quantity=items_by_id[match.catalogue_item_id][0].available_quantity,
            last_verified_at=(
                items_by_id[match.catalogue_item_id][0].last_verified_at
                or items_by_id[match.catalogue_item_id][2].last_verified_at
            ),
            catalogue_age_days=_catalogue_age_days(
                items_by_id[match.catalogue_item_id][0].last_verified_at
                or items_by_id[match.catalogue_item_id][2].last_verified_at
            ),
            match_score=match.score,
            reasons=match.reasons,
        )
        for match in ranked
    ]


def _catalogue_age_days(last_verified_at: datetime | None) -> int | None:
    if last_verified_at is None:
        return None
    if last_verified_at.tzinfo is None:
        last_verified_at = last_verified_at.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - last_verified_at).days)