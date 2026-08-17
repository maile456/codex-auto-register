import os
os.environ["PAYPAL_WEB_ENABLE_DYNAMIC_COUNTRIES"] = "1"
from unittest.mock import patch
import web


def test_dynamic_country_job_gate():
    assert web.ENABLE_DYNAMIC_COUNTRIES is True
    assert "SG" in web.supported_country_codes()
    with patch("threading.Thread.start", return_value=None):
        job = web.create_job(
            owner_device_id="fixture-device",
            ba_token="BA-ABCDEFGH123456",  # gitleaks:allow
            phone="+6581234567",
            debug=False,
            max_card_attempts=1,
            country="SG",
            proxy_pool=["http://127.0.0.1:9999"],
        )
    assert job.country == "SG"
    assert job._proxy_config.entry.host == "127.0.0.1"
    with web.JOBS_LOCK:
        web.JOBS.pop(job.id, None)


if __name__ == "__main__":
    test_dynamic_country_job_gate()
    print("DYNAMIC_COUNTRY_GATE_TEST_OK")
