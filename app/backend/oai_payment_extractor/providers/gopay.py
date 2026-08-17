from .base import ProviderAdapter


GOPAY = ProviderAdapter(
    name="gopay",
    result_field="gopay_url",
    preferred_hosts=(
        "app.midtrans.com",
        "gopay.co.id",
        "app.gopay.co.id",
        "gojek.link",
        "gopayapp.page.link",
        "gojek.page.link",
    ),
)
