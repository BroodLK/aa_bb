"""Shared configuration for the Django-ESI OpenAPI 3.1 client."""

from __future__ import annotations

import logging
from datetime import timezone as dt_timezone
from typing import Any

from django.utils import timezone
from email.utils import parsedate_to_datetime
from esi.openapi_clients import ESIClientProvider, EsiOperation
from esi.exceptions import HTTPClientError, HTTPNotModified, HTTPServerError
from aiopenapi3 import ContentTypeError
from httpx import Response

from . import __title__, __version__, __github_url__, __esi_compatibility_date__

DEFAULT_OPERATIONS = [
    "PostUniverseNames",
    "PostUniverseIds",
    "GetCharactersCharacterIdCorporationhistory",
    "GetCorporationsCorporationId",
    "GetCorporationsCorporationIdAlliancehistory",
    "GetAlliancesAllianceId",
    "GetSovereigntyMap",
    "GetKillmailsKillmailIdKillmailHash",
]


logger = logging.getLogger(__name__)

esi = ESIClientProvider(
    compatibility_date=__esi_compatibility_date__,
    ua_appname=__title__,
    ua_version=__version__,
    ua_url=__github_url__,
    operations=DEFAULT_OPERATIONS,
)


def to_plain(value):
    """Recursively convert Pydantic models returned by the OpenAPI client to plain Python types."""
    if hasattr(value, "model_dump"):
        return to_plain(value.model_dump())
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: to_plain(val) for key, val in value.items()}
    return value


def parse_expires(headers: dict | None):
    """Extract a timezone-aware datetime from HTTP Expires headers (if present)."""
    if not headers:
        return None
    value = headers.get("Expires")
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(dt_timezone.utc)


def _parse_response_expires(response):
    if response is None:
        return None
    if isinstance(response, dict):
        return parse_expires(response)
    return parse_expires(response.headers)


def call_result(operation, **kwargs):
    """Execute an OpenAPI operation.result() call and return (data, expires_at)."""
    return ESIHandler.result_with_expiry(operation, **kwargs)


def call_results(operation, **kwargs):
    """Execute operation.results() and return (list_data, expires_at) with plain types."""
    return ESIHandler.results_with_expiry(operation, **kwargs)


class ESIHandler:
    """
    Wrapper for ESI OpenAPI operations that can optionally swallow common
    ESI exceptions (304, content-type errors, client/server errors).
    """

    @classmethod
    def result(
        cls,
        operation: EsiOperation,
        *,
        use_etag: bool = True,
        return_response: bool = False,
        force_refresh: bool = False,
        use_cache: bool = True,
        allow_not_modified: bool = False,
        swallow_errors: bool = False,
        **extra: Any,
    ) -> tuple[Any, Response] | Any | None:
        try:
            return operation.result(
                use_etag=use_etag,
                return_response=return_response,
                force_refresh=force_refresh,
                use_cache=use_cache,
                **extra,
            )
        except HTTPNotModified as exc:
            if allow_not_modified:
                logger.debug(
                    "ESI 304 Not Modified for operation %s",
                    operation.operation.operationId,
                )
                if return_response:
                    headers = getattr(exc, "headers", None)
                    return None, dict(headers) if headers is not None else {}
                return None
            raise
        except ContentTypeError:
            if swallow_errors:
                logger.warning(
                    "ESI returned unexpected content type for operation %s",
                    operation.operation.operationId,
                )
                return None
            raise
        except (HTTPClientError, HTTPServerError) as exc:
            if swallow_errors:
                logger.warning(
                    "ESI error for operation %s: %s",
                    operation.operation.operationId,
                    exc,
                )
                return None
            raise

    @classmethod
    def results(
        cls,
        operation: EsiOperation,
        *,
        use_etag: bool = True,
        return_response: bool = False,
        force_refresh: bool = False,
        use_cache: bool = True,
        allow_not_modified: bool = False,
        swallow_errors: bool = False,
        **extra: Any,
    ) -> tuple[Any, Response] | Any | None:
        try:
            return operation.results(
                use_etag=use_etag,
                return_response=return_response,
                force_refresh=force_refresh,
                use_cache=use_cache,
                **extra,
            )
        except HTTPNotModified as exc:
            if allow_not_modified:
                logger.debug(
                    "ESI 304 Not Modified for operation %s",
                    operation.operation.operationId,
                )
                if return_response:
                    headers = getattr(exc, "headers", None)
                    return None, dict(headers) if headers is not None else {}
                return None
            raise
        except ContentTypeError:
            if swallow_errors:
                logger.warning(
                    "ESI returned unexpected content type for operation %s",
                    operation.operation.operationId,
                )
                return None
            raise
        except (HTTPClientError, HTTPServerError) as exc:
            if swallow_errors:
                logger.warning(
                    "ESI error for operation %s: %s",
                    operation.operation.operationId,
                    exc,
                )
                return None
            raise

    @classmethod
    def result_plain(
        cls,
        operation: EsiOperation,
        **kwargs: Any,
    ) -> Any | None:
        result = cls.result(operation, **kwargs)
        if result is None:
            return None
        return to_plain(result)

    @classmethod
    def results_plain(
        cls,
        operation: EsiOperation,
        **kwargs: Any,
    ) -> Any | None:
        result = cls.results(operation, **kwargs)
        if result is None:
            return None
        return to_plain(result)

    @classmethod
    def result_with_expiry(
        cls,
        operation: EsiOperation,
        **kwargs: Any,
    ) -> tuple[Any | None, timezone.datetime | None]:
        result = cls.result(operation, return_response=True, **kwargs)
        if result is None:
            return None, None
        data, response = result
        return to_plain(data), _parse_response_expires(response)

    @classmethod
    def results_with_expiry(
        cls,
        operation: EsiOperation,
        **kwargs: Any,
    ) -> tuple[Any | None, timezone.datetime | None]:
        result = cls.results(operation, return_response=True, **kwargs)
        if result is None:
            return None, None
        data, response = result
        return to_plain(data), _parse_response_expires(response)

    @classmethod
    def post_universe_names(
        cls,
        ids: list[int],
        *,
        operation_kwargs: dict | None = None,
        **result_kwargs: Any,
    ) -> Any | None:
        op_kwargs = operation_kwargs or {}
        operation = esi.client.Universe.PostUniverseNames(body=ids, **op_kwargs)
        return cls.result_plain(operation, **result_kwargs)

    @classmethod
    def post_universe_ids(
        cls,
        names: list[str],
        *,
        operation_kwargs: dict | None = None,
        **result_kwargs: Any,
    ) -> Any | None:
        op_kwargs = operation_kwargs or {}
        operation = esi.client.Universe.PostUniverseIds(body=names, **op_kwargs)
        return cls.result_plain(operation, **result_kwargs)

    @classmethod
    def get_corporations_corporation_id_with_expiry(
        cls,
        corporation_id: int,
        **result_kwargs: Any,
    ) -> tuple[Any | None, timezone.datetime | None]:
        operation = esi.client.Corporation.GetCorporationsCorporationId(
            corporation_id=corporation_id
        )
        return cls.result_with_expiry(operation, **result_kwargs)

    @classmethod
    def get_alliances_alliance_id_with_expiry(
        cls,
        alliance_id: int,
        **result_kwargs: Any,
    ) -> tuple[Any | None, timezone.datetime | None]:
        operation = esi.client.Alliance.GetAlliancesAllianceId(
            alliance_id=alliance_id
        )
        return cls.result_with_expiry(operation, **result_kwargs)

    @classmethod
    def get_characters_corporation_history_with_expiry(
        cls,
        character_id: int,
        **result_kwargs: Any,
    ) -> tuple[Any | None, timezone.datetime | None]:
        operation = esi.client.Character.GetCharactersCharacterIdCorporationhistory(
            character_id=character_id
        )
        return cls.results_with_expiry(operation, **result_kwargs)

    @classmethod
    def get_corporations_alliance_history_with_expiry(
        cls,
        corporation_id: int,
        **result_kwargs: Any,
    ) -> tuple[Any | None, timezone.datetime | None]:
        operation = esi.client.Corporation.GetCorporationsCorporationIdAlliancehistory(
            corporation_id=corporation_id
        )
        return cls.results_with_expiry(operation, **result_kwargs)

    @classmethod
    def get_sovereignty_map_with_expiry(
        cls,
        *,
        operation_kwargs: dict | None = None,
        **result_kwargs: Any,
    ) -> tuple[Any | None, timezone.datetime | None]:
        op_kwargs = operation_kwargs or {}
        operation = esi.client.Sovereignty.GetSovereigntyMap(**op_kwargs)
        return cls.results_with_expiry(operation, **result_kwargs)

    @classmethod
    def get_killmails_killmail_id_killmail_hash(
        cls,
        killmail_id: int,
        killmail_hash: str,
        *,
        operation_kwargs: dict | None = None,
        **result_kwargs: Any,
    ) -> Any | None:
        op_kwargs = operation_kwargs or {}
        operation = esi.client.Killmails.GetKillmailsKillmailIdKillmailHash(
            killmail_id=killmail_id,
            killmail_hash=killmail_hash,
            **op_kwargs,
        )
        return cls.result_plain(operation, **result_kwargs)
