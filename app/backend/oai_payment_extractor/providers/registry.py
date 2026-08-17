from __future__ import annotations

from .base import ProviderAdapter
from .gcash import GCASH
from .gopay import GOPAY
from .paypal import PAYPAL


PROVIDERS: dict[str, ProviderAdapter] = {
    provider.name: provider for provider in (PAYPAL, GOPAY, GCASH)
}


def get_provider(payment_method: str) -> ProviderAdapter:
    key = str(payment_method or "paypal").strip().lower() or "paypal"
    try:
        return PROVIDERS[key]
    except KeyError as exc:
        raise ValueError(
            f"payment_method must be one of {', '.join(PROVIDERS)}"
        ) from exc


def provider_redirect_config(payment_method: str) -> dict[str, object]:
    provider = get_provider(payment_method)
    return {
        "preferred_hosts": provider.preferred_hosts,
        "result_field": provider.result_field,
    }
