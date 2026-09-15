"""Stable, dependency-lazy public surface for GrooveMap runtime libraries."""

from importlib import import_module
from pkgutil import extend_path
from typing import TYPE_CHECKING, Any


__path__ = extend_path(__path__, __name__)

if TYPE_CHECKING:
    from common.batch import AsyncBatchEngine, BatchItemResult, BatchObserver, BatchPolicy, BatchSink
    from common.config import neo4j_security_kwargs, parse_postgres_host_port, setup_logging
    from common.data_normalizer import normalize_record
    from common.db_resilience import (
        AsyncResilientConnection,
        CircuitBreaker,
        CircuitBreakerConfig,
        CircuitOpenError,
        CircuitState,
        ConnectionEstablishmentError,
        DatabaseUnavailableError,
        ExponentialBackoff,
        ResilientConnection,
        async_resilient_connection,
        resilient_connection,
    )
    from common.delivery import Delivery, DeliveryObserver, DeliveryResult, FailureClassifier, FailureKind, Settlement, run_delivery
    from common.errors import describe_exception
    from common.events import (
        Event,
        EventValidationError,
        Impression,
        consent_purposes,
        event_types,
        is_valid_event_type,
        new_event,
        new_impression,
        payload_schema_for,
        surfaces,
        validate_event,
        validate_impression,
    )
    from common.health_server import HealthServer
    from common.identifiers import (
        IdentifierValidationError,
        alias_identifier_types,
        alias_refs_for_release,
        company_role_categories,
        identifier_types,
        validate_companies_block,
        validate_identifiers_block,
    )
    from common.identity import (
        AliasRef,
        alias_sources,
        attach_aliases,
        catalog_kinds,
        entity_kinds,
        is_valid_alias_source,
        is_valid_entity_kind,
        is_valid_provider,
        new_id,
        providers,
        resolve_aliases,
    )
    from common.media import (
        families_of,
        family_ids,
        flatten_descriptions,
        legacy_format_names_to_media,
        map_discogs_formats,
        map_musicbrainz_release,
        medium_ids,
        medium_label,
    )
    from common.neo4j_resilient import AsyncResilientNeo4jDriver, ResilientNeo4jDriver, with_async_neo4j_retry, with_neo4j_retry
    from common.outage_backoff import OutageBackoff
    from common.postgres_resilient import AsyncPostgreSQLPool, AsyncResilientPostgreSQL
    from common.query_debug import execute_sql, is_db_profiling, is_debug, log_cypher_query, log_sql_query
    from common.rabbitmq_resilient import AsyncResilientRabbitMQ, ResilientRabbitMQConnection, process_message_with_retry
    from common.telemetry import (
        get_meter,
        instrument_fastapi_app,
        instrument_httpx,
        setup_telemetry,
        shutdown_telemetry,
        start_event_loop_monitor,
    )
    from common.tracing import extract_context, flush_span, get_tracer, inject_headers


_EXPORTS: dict[str, tuple[str, str]] = {
    "AsyncBatchEngine": ("common.batch", "AsyncBatchEngine"),
    "BatchItemResult": ("common.batch", "BatchItemResult"),
    "BatchObserver": ("common.batch", "BatchObserver"),
    "BatchPolicy": ("common.batch", "BatchPolicy"),
    "BatchSink": ("common.batch", "BatchSink"),
    "neo4j_security_kwargs": ("common.config", "neo4j_security_kwargs"),
    "parse_postgres_host_port": ("common.config", "parse_postgres_host_port"),
    "setup_logging": ("common.config", "setup_logging"),
    "normalize_record": ("common.data_normalizer", "normalize_record"),
    "AsyncResilientConnection": ("common.db_resilience", "AsyncResilientConnection"),
    "CircuitBreaker": ("common.db_resilience", "CircuitBreaker"),
    "CircuitBreakerConfig": ("common.db_resilience", "CircuitBreakerConfig"),
    "CircuitOpenError": ("common.db_resilience", "CircuitOpenError"),
    "CircuitState": ("common.db_resilience", "CircuitState"),
    "ConnectionEstablishmentError": ("common.db_resilience", "ConnectionEstablishmentError"),
    "DatabaseUnavailableError": ("common.db_resilience", "DatabaseUnavailableError"),
    "ExponentialBackoff": ("common.db_resilience", "ExponentialBackoff"),
    "ResilientConnection": ("common.db_resilience", "ResilientConnection"),
    "async_resilient_connection": ("common.db_resilience", "async_resilient_connection"),
    "resilient_connection": ("common.db_resilience", "resilient_connection"),
    "describe_exception": ("common.errors", "describe_exception"),
    "Delivery": ("common.delivery", "Delivery"),
    "DeliveryObserver": ("common.delivery", "DeliveryObserver"),
    "DeliveryResult": ("common.delivery", "DeliveryResult"),
    "FailureClassifier": ("common.delivery", "FailureClassifier"),
    "FailureKind": ("common.delivery", "FailureKind"),
    "Settlement": ("common.delivery", "Settlement"),
    "run_delivery": ("common.delivery", "run_delivery"),
    "Event": ("common.events", "Event"),
    "EventValidationError": ("common.events", "EventValidationError"),
    "Impression": ("common.events", "Impression"),
    "consent_purposes": ("common.events", "consent_purposes"),
    "event_types": ("common.events", "event_types"),
    "is_valid_event_type": ("common.events", "is_valid_event_type"),
    "new_event": ("common.events", "new_event"),
    "new_impression": ("common.events", "new_impression"),
    "payload_schema_for": ("common.events", "payload_schema_for"),
    "surfaces": ("common.events", "surfaces"),
    "validate_event": ("common.events", "validate_event"),
    "validate_impression": ("common.events", "validate_impression"),
    "HealthServer": ("common.health_server", "HealthServer"),
    "IdentifierValidationError": ("common.identifiers", "IdentifierValidationError"),
    "alias_identifier_types": ("common.identifiers", "alias_identifier_types"),
    "alias_refs_for_release": ("common.identifiers", "alias_refs_for_release"),
    "company_role_categories": ("common.identifiers", "company_role_categories"),
    "identifier_types": ("common.identifiers", "identifier_types"),
    "validate_companies_block": ("common.identifiers", "validate_companies_block"),
    "validate_identifiers_block": ("common.identifiers", "validate_identifiers_block"),
    "AliasRef": ("common.identity", "AliasRef"),
    "alias_sources": ("common.identity", "alias_sources"),
    "attach_aliases": ("common.identity", "attach_aliases"),
    "catalog_kinds": ("common.identity", "catalog_kinds"),
    "entity_kinds": ("common.identity", "entity_kinds"),
    "is_valid_alias_source": ("common.identity", "is_valid_alias_source"),
    "is_valid_entity_kind": ("common.identity", "is_valid_entity_kind"),
    "is_valid_provider": ("common.identity", "is_valid_provider"),
    "new_id": ("common.identity", "new_id"),
    "providers": ("common.identity", "providers"),
    "resolve_aliases": ("common.identity", "resolve_aliases"),
    "families_of": ("common.media", "families_of"),
    "family_ids": ("common.media", "family_ids"),
    "flatten_descriptions": ("common.media", "flatten_descriptions"),
    "legacy_format_names_to_media": ("common.media", "legacy_format_names_to_media"),
    "map_discogs_formats": ("common.media", "map_discogs_formats"),
    "map_musicbrainz_release": ("common.media", "map_musicbrainz_release"),
    "medium_ids": ("common.media", "medium_ids"),
    "medium_label": ("common.media", "medium_label"),
    "AsyncResilientNeo4jDriver": ("common.neo4j_resilient", "AsyncResilientNeo4jDriver"),
    "ResilientNeo4jDriver": ("common.neo4j_resilient", "ResilientNeo4jDriver"),
    "with_async_neo4j_retry": ("common.neo4j_resilient", "with_async_neo4j_retry"),
    "with_neo4j_retry": ("common.neo4j_resilient", "with_neo4j_retry"),
    "OutageBackoff": ("common.outage_backoff", "OutageBackoff"),
    "AsyncPostgreSQLPool": ("common.postgres_resilient", "AsyncPostgreSQLPool"),
    "AsyncResilientPostgreSQL": ("common.postgres_resilient", "AsyncResilientPostgreSQL"),
    "execute_sql": ("common.query_debug", "execute_sql"),
    "is_db_profiling": ("common.query_debug", "is_db_profiling"),
    "is_debug": ("common.query_debug", "is_debug"),
    "log_cypher_query": ("common.query_debug", "log_cypher_query"),
    "log_sql_query": ("common.query_debug", "log_sql_query"),
    "AsyncResilientRabbitMQ": ("common.rabbitmq_resilient", "AsyncResilientRabbitMQ"),
    "ResilientRabbitMQConnection": ("common.rabbitmq_resilient", "ResilientRabbitMQConnection"),
    "process_message_with_retry": ("common.rabbitmq_resilient", "process_message_with_retry"),
    "get_meter": ("common.telemetry", "get_meter"),
    "instrument_fastapi_app": ("common.telemetry", "instrument_fastapi_app"),
    "instrument_httpx": ("common.telemetry", "instrument_httpx"),
    "setup_telemetry": ("common.telemetry", "setup_telemetry"),
    "shutdown_telemetry": ("common.telemetry", "shutdown_telemetry"),
    "start_event_loop_monitor": ("common.telemetry", "start_event_loop_monitor"),
    "extract_context": ("common.tracing", "extract_context"),
    "flush_span": ("common.tracing", "flush_span"),
    "get_tracer": ("common.tracing", "get_tracer"),
    "inject_headers": ("common.tracing", "inject_headers"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Load an exported capability only when its dependency family is requested."""
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
