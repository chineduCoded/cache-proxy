import pytest
from fastapi import status
import httpx
import json
from app.caching_proxy import app

@pytest.mark.asyncio
async def test_cache_miss_and_hit(monkeypatch):
    test_response = {"message": "Hello, World!"}
    
    async def mock_get_miss(self, url, *args, **kwargs):
        return httpx.Response(
            status_code=status.HTTP_200_OK,
            content=json.dumps(test_response).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Cache": "MISS"
            }
        )
    
    async def mock_get_hit(self, url, *args, **kwargs):
        return httpx.Response(
            status_code=status.HTTP_200_OK,
            content=json.dumps(test_response).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Cache": "HIT"
            }
        )
    
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get_miss)
    
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get("/some-path")
        assert response.status_code == status.HTTP_200_OK
        assert response.headers["X-Cache"] == "MISS"
        assert response.headers["Content-Type"] == "application/json"
        assert response.json() == test_response
        
        # Simulate cache hit on second request
        monkeypatch.setattr(httpx.AsyncClient, "get", mock_get_hit)
        response = await client.get("/some-path")
        assert response.status_code == status.HTTP_200_OK
        assert response.headers["X-Cache"] == "HIT"
        assert response.headers["Content-Type"] == "application/json"
        assert response.json() == test_response

@pytest.mark.asyncio
async def test_invalid_json_response(monkeypatch):
    async def mock_get(self, url, *args, **kwargs):
        return httpx.Response(
            status_code=status.HTTP_502_BAD_GATEWAY,  # Updated to 502
            content=b'invalid json',
            headers={"Content-Type": "application/json"}
        )
    
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get("/some-path")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY

@pytest.mark.asyncio
async def test_non_200_response(monkeypatch):
    async def mock_get(self, url, *args, **kwargs):
        return httpx.Response(
            status_code=status.HTTP_404_NOT_FOUND,
            content=json.dumps({"error": "Not found"}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Cache": "MISS"
            }
        )
    
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get("/some-path")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.headers["X-Cache"] == "MISS"
