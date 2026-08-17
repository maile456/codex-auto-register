import os
os.environ["PAYPAL_WEB_ENABLE_DYNAMIC_COUNTRIES"] = "1"
from unittest.mock import patch
import web
from paypal.models import CardInfo
from paypal.proxy import ProxyConfig, ProxyEntry


class FakeFlow:
    def __init__(self, **kwargs):
        self.job = kwargs["job"]
        self.address = kwargs["address"]
        self.user = kwargs["user"]
        assert self.address.country == "SG"
        assert self.user.phone_country_code == "+65"
    def run(self):
        self.job.runtime_schema = {"country": "SG", "source": "fixture"}
        return {"status": "success", "return_url": "https://merchant.fixture/return"}
    def close(self):
        return None


class FakeResponse:
    status_code = 404
    text = ""
    def json(self):
        return {}


class FakeClient:
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def post(self, *args, **kwargs): return FakeResponse()


def test_run_job_dynamic_country_fixture():
    proxy = ProxyConfig(True, ProxyEntry("127.0.0.1", 9999, "", ""))
    job = web.WebJob(
        id="fixturejob",
        owner_device_id="fixture-device",
        ba_token="BA-ABCDEFGH123456",  # gitleaks:allow
        phone="+6581234567",
        country="SG",
        buyer_mode="identity_elevation",
        max_card_attempts=1,
        proxy_enabled=True,
        _proxy_config=proxy,
        _proxy_pool=["http://127.0.0.1:9999"],
    )
    with patch("web.select_working_proxy", return_value=proxy), patch(
        "web.generate_card", return_value=CardInfo("4111111111111111", "12/2030", "123")
    ), patch("web.WebIdentityElevationPayPalFlow", FakeFlow), patch("web.httpx.Client", FakeClient):
        web.run_job(job)
    assert job.status == "completed"
    assert job.result["status"] == "success"
    assert job.runtime_schema["country"] == "SG"
    assert job.generated["address"]["country"] == "SG"


if __name__ == "__main__":
    test_run_job_dynamic_country_fixture()
    print("RUN_JOB_DYNAMIC_FIXTURE_OK")
