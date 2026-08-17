from .base import ProviderAdapter


PAYPAL = ProviderAdapter(
    name="paypal",
    result_field="paypal_url",
    preferred_hosts=("paypal.com",),
)
