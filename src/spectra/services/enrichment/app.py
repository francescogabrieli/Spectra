"""Private local service for categorization providers and exchange rates."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException

from spectra.ai import categorise
from spectra.config import load_settings
from spectra.fx import FXConversionError, convert_currency_result
from spectra.local_categorizer import categorise_local
from spectra.ml_classifier import train_classifier
from spectra.services.contracts import CategorizeRequest, FxConvertRequest
from spectra.services.enrichment.storage import FXRateCache

logger = logging.getLogger("spectra.services.enrichment")
app = FastAPI(title="Spectra Enrichment", version="1.0", docs_url=None, redoc_url=None)


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"service": "enrichment", "status": "live"}


@app.get("/health/ready")
def ready() -> dict[str, str]:
    settings = load_settings()
    try:
        with FXRateCache(settings.fx_db_path):
            pass
    except Exception as exc:
        logger.error("Enrichment readiness check failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Enrichment cache is unavailable") from exc
    return {"service": "enrichment", "status": "ready"}


@app.get("/internal/v1/enrichment/provider-status")
def provider_status() -> dict[str, Any]:
    """Expose readiness without sending provider credentials to other services."""
    settings = load_settings()
    provider = settings.ai_provider
    if provider == "local":
        configured = True
    elif provider == "openai":
        configured = bool(settings.openai_api_key)
    else:
        configured = bool(settings.gemini_api_key)
    return {"provider": provider, "ready": configured}


@app.post("/internal/v1/enrichment/categorize")
def categorize(request: CategorizeRequest) -> list[dict[str, Any]]:
    settings = load_settings()
    provider = str(request.provider or "local").strip().lower()
    sources = [dict(row) for row in request.transactions]
    try:
        if provider == "local":
            classifier = train_classifier(request.training_data)
            results = categorise_local(
                sources,
                merchant_db=request.merchant_categories,
                ml_classifier=classifier,
            )
        elif provider in {"openai", "gemini"}:
            api_key = settings.openai_api_key if provider == "openai" else settings.gemini_api_key
            model = settings.openai_model if provider == "openai" else settings.gemini_model
            if not api_key:
                raise HTTPException(status_code=503, detail=f"{provider.title()} credentials are not configured")
            results = categorise(
                sources,
                request.categories,
                provider=provider,
                api_key=api_key,
                model=model,
                base_currency=request.base_currency,
            )
        else:
            raise HTTPException(status_code=400, detail="Unsupported categorization provider")
        return [tx.model_dump(mode="json") for tx in results]
    except HTTPException:
        raise
    except Exception as exc:
        # Avoid logging statement rows or model output.
        logger.warning("Categorization failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Categorization service failed") from exc


@app.post("/internal/v1/enrichment/fx/convert")
def convert(request: FxConvertRequest) -> dict[str, str]:
    settings = load_settings()
    try:
        if request.persist_rate:
            cache_context = FXRateCache(settings.fx_db_path)
        else:
            cache_context = None
        try:
            result = convert_currency_result(
                request.amount,
                request.from_currency,
                request.to_currency,
                request.rate_date,
                rate_store=cache_context,
            )
        finally:
            if cache_context is not None:
                cache_context.close()
    except FXConversionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid FX conversion request") from exc
    except Exception as exc:
        logger.warning("FX conversion failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="FX conversion service unavailable") from exc
    return {
        "amount": format(result.amount, "f"),
        "rate": format(result.rate, "f"),
        "requested_date": result.requested_date,
        "effective_date": result.effective_date,
        "source": result.source,
    }

