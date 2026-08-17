from .base import ProviderAdapter


GCASH = ProviderAdapter(
    name="gcash",
    result_field="gcash_url",
    preferred_hosts=("gcash.com",),
)
