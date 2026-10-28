"""The central provider registry: one :class:`ProviderSpec` per provider id, deterministic order.

Adding a provider means registering one spec (see ``lint_providers_catalog``) plus its test row. The
registry is process-global on purpose: it is the extension point, so duplicates fail loudly.
"""

from __future__ import annotations

from .lint_providers_model import ProviderSpec

_REGISTRY: dict[str, ProviderSpec] = {}
PROVIDER_IDS: frozenset[str] = frozenset()


def register(spec: ProviderSpec) -> ProviderSpec:
    global PROVIDER_IDS
    if spec.id in _REGISTRY:
        raise ValueError(f"lint provider {spec.id!r} is already registered")
    _REGISTRY[spec.id] = spec
    PROVIDER_IDS = frozenset(_REGISTRY)
    return spec


def providers() -> tuple[ProviderSpec, ...]:
    return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))


def provider(provider_id: str) -> ProviderSpec:
    return _REGISTRY[provider_id]
