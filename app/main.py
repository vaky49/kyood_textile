from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.approvals import router as approvals_router
from app.api.routes.catalogues import router as catalogues_router
from app.api.routes.media import router as media_router
from app.api.routes.requests import router as requests_router
from app.api.routes.rfqs import router as rfqs_router
from app.api.routes.suppliers import router as suppliers_router
from app.api.routes.whatsapp import router as whatsapp_router
from app.settings import get_settings

settings = get_settings()

app = FastAPI(
    title="Textile Procurement Agent",
    description="Human-assisted supplier discovery and RFQ workflow.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.frontend_origins.split(",") if origin.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "X-Operator-Token", "X-Operator-ID", "X-Hub-Signature-256"],
)
app.include_router(suppliers_router)
app.include_router(catalogues_router)
app.include_router(approvals_router)
app.include_router(requests_router)
app.include_router(rfqs_router)
app.include_router(media_router)
app.include_router(whatsapp_router)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}