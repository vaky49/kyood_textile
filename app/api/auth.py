from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.settings import Settings, get_settings

operator_token_header = APIKeyHeader(name="X-Operator-Token", auto_error=False)


async def require_operator(
    token: Annotated[str | None, Security(operator_token_header)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    expected_token = settings.operator_api_token
    if expected_token is None or token is None or not compare_digest(token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid operator credentials are required",
            headers={"WWW-Authenticate": "ApiKey"},
        )