"""Bounded refresh retry support for the single-process Google MCP server.

FastMCP 4.0.3 deletes rotated tokens immediately (upstream issue #4901).
Replay the same response for 60 seconds, only while its successor is live.
The response uses the existing encrypted, persistent OAuth storage.
"""

import asyncio
import hashlib
import time

from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.server.auth.provider import AccessToken, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


class RetrySafeGoogleProvider(GoogleProvider):
    _retry_collection = "google-refresh-retries"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Deployment runs one process. Include load/revoke in the same lock so
        # no request observes the gap between rotation and response persistence.
        self._refresh_lock = asyncio.Lock()

    @staticmethod
    def _token_key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    async def _retry(self, client, token):
        record = await self._client_storage.get(
            key=self._token_key(token), collection=self._retry_collection
        )
        if not record or record["expires_at"] <= time.time():
            return None
        if record["client_id"] != client.client_id:
            return None

        # Do not resurrect a consumed, expired, or revoked successor.
        successor = record["response"]["refresh_token"]
        metadata = await self._refresh_token_store.get(
            key=self._token_key(successor)
        )
        if not metadata or metadata.client_id != client.client_id:
            return None
        if (
            metadata.expires_at is not None
            and metadata.expires_at <= time.time()
        ):
            return None
        payload = self.jwt_issuer.verify_token(
            successor, expected_token_use="refresh"
        )
        if not await self._jti_mapping_store.get(key=payload["jti"]):
            return None
        return record

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        async with self._refresh_lock:
            record = await self._retry(client, refresh_token)
            if record:
                return RefreshToken(
                    token=refresh_token,
                    client_id=record["client_id"],
                    scopes=record["original_scopes"],
                    expires_at=record["original_expires_at"],
                )
            return await super().load_refresh_token(client, refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        async with self._refresh_lock:
            record = await self._retry(client, refresh_token.token)
            if record:
                if sorted(scopes) != record["requested_scopes"]:
                    raise TokenError("invalid_scope", "Retry scopes must match")
                return OAuthToken.model_validate(record["response"])

            # load_refresh_token may have run before another request rotated it.
            current = await super().load_refresh_token(
                client, refresh_token.token
            )
            if current is None:
                raise TokenError(
                    "invalid_grant", "Refresh token is no longer valid"
                )

            response = await super().exchange_refresh_token(
                client, current, scopes
            )
            ttl = (
                min(60, int(current.expires_at - time.time()))
                if current.expires_at
                else 60
            )
            if response.refresh_token and ttl > 0:
                await self._client_storage.put(
                    key=self._token_key(refresh_token.token),
                    collection=self._retry_collection,
                    value={
                        "client_id": client.client_id,
                        "original_scopes": current.scopes,
                        "original_expires_at": current.expires_at,
                        "requested_scopes": sorted(scopes),
                        "expires_at": time.time() + ttl,
                        "response": response.model_dump(mode="json"),
                    },
                    ttl=ttl,
                )
            return response

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with self._refresh_lock:
            await self._client_storage.delete(
                key=self._token_key(token.token),
                collection=self._retry_collection,
            )
            await super().revoke_token(token)
