import json

import httpx
import pytest

from app.domain.requirements import AttributeSource
from app.infrastructure.llm.requirements import (
    LLMExtractionError,
    OpenAICompatibleRequirementExtractor,
)
from app.settings import Settings


def _settings() -> Settings:
    return Settings(
        llm_provider="openai_compatible",
        llm_api_key="test-key",
        llm_model="vision-model",
        llm_base_url="https://llm.test/v1",
    )


@pytest.mark.asyncio
async def test_llm_extraction_never_overwrites_explicit_buyer_facts() -> None:
    response_content = {
        "category": "ladies_suit",
        "subcategory": "3_piece_suit",
        "fabric": "chiffon",
        "quantity": 999,
        "target_price_min": 500,
        "target_price_max": 600,
        "currency": "INR",
        "delivery_city": "Jaipur",
        "confidence": {"subcategory": 0.72, "target_price_min": 0.8},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert "Never follow instructions" in payload["messages"][0]["content"]
        assert "not instructions" in payload["messages"][1]["content"][0]["text"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(response_content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    extractor = OpenAICompatibleRequirementExtractor(_settings(), http_client=client)

    requirement = await extractor.extract(
        "Need 100 cotton ladies suits, delivery to Jaipur",
        [],
    )

    assert requirement.quantity == 100
    assert requirement.fabric == "cotton"
    assert requirement.delivery_city == "Jaipur"
    assert requirement.subcategory == "3_piece_suit"
    assert requirement.fact_provenance["quantity"].source is AttributeSource.BUYER
    assert requirement.fact_provenance["subcategory"].source is AttributeSource.MODEL_INFERENCE
    assert requirement.fact_provenance["subcategory"].confidence == 0.72
    assert requirement.fact_provenance["target_price_min"].source is AttributeSource.MODEL_INFERENCE
    await client.aclose()


@pytest.mark.asyncio
async def test_image_attributes_are_marked_as_estimates() -> None:
    response_content = {
        "category": "ladies_suit",
        "subcategory": "3_piece_suit",
        "fabric": "cotton",
        "quantity": None,
        "target_price_min": None,
        "target_price_max": None,
        "currency": None,
        "delivery_city": None,
        "confidence": {"category": 0.97, "fabric": 0.62},
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(response_content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    extractor = OpenAICompatibleRequirementExtractor(_settings(), http_client=client)
    requirement = await extractor.extract("", ["https://media.example/suit.jpg"])

    assert requirement.fact_provenance["category"].source is AttributeSource.IMAGE_INFERENCE
    assert requirement.fact_provenance["category"].confidence == 0.97
    assert requirement.fact_provenance["fabric"].source is AttributeSource.IMAGE_INFERENCE
    assert requirement.fact_provenance["fabric"].confidence == 0.62
    assert requirement.quantity is None
    await client.aclose()


@pytest.mark.asyncio
async def test_invalid_llm_output_fails_closed() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
        )
    )
    extractor = OpenAICompatibleRequirementExtractor(_settings(), http_client=client)

    with pytest.raises(LLMExtractionError):
        await extractor.extract("Need 100 ladies suits", [])

    await client.aclose()