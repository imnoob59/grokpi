"""Security utilities untuk API"""

from typing import Optional

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings


bearer_scheme = HTTPBearer(auto_error=False)


def require_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
) -> bool:
    """Validasi API key via skema Bearer pada OpenAPI/Swagger"""
    if not settings.API_KEY:
        return True

    if not credentials:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    if credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization format")

    token = credentials.credentials
    if token != settings.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return True
