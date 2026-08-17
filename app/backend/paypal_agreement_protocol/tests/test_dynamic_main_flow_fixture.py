from unittest.mock import patch
from paypal.flow import PayPalFlow
from paypal.models import BillingAddress, CardInfo, SessionState, generate_user
from web import WebPayPalFlow


class FakeJob:
    def __init__(self):
        self.runtime_schema = None
        self.generated = None
    def check_cancelled(self):
        return None
    def set_status(self, status, stage=None):
        return None
    def set_generated(self, value):
        self.generated = value


class FakeSession:
    def graphql(self, operation, query, variables):
        assert operation == "GriffinMetadataQuery"
        assert variables["countryCode"] == "SG"
        return {"data": {"localeMetadata": {
            "currencyCode": "SGD",
            "address": {"layout": [
                {"name": "line1", "isRequired": True},
                {"name": "line2", "isRequired": False},
                {"name": "postcode", "isRequired": True, "minLength": 6, "maxLength": 6, "regex": "\\d{6}"},
            ]},
            "phone": {"masks": {"mobile": "0000 0000"}, "patterns": {"default": "[3689]\\d{7}"}},
        }}}


def fake_phase2(self):
    self._signup_html = '{"fieldName":"occupation","isRequired":true}'
    self.state.ec_token = "EC-FIXTURE"
    self.state.signup_url = "https://www.paypal.com/checkoutweb/signup?token=EC-FIXTURE"
    self.state.content_identifier = "SG:en:fixture:compliance.signupTerms"
    return None


def test_dynamic_country_main_flow_fixture():
    flow = WebPayPalFlow.__new__(WebPayPalFlow)
    flow.job = FakeJob()
    flow.country = "SG"
    flow.lang = "en"
    flow.session = FakeSession()
    flow.user = generate_user("+6581234567", "SG")
    flow.user.email = "fixture@example.com"
    flow.user.occupation = "BUSINESS"
    flow.card = CardInfo("4111111111111111", "12/2030", "123")
    flow.address = BillingAddress("", "", "", "", "", "", "SG")
    flow.state = SessionState()
    with patch.object(PayPalFlow, "_phase2_create_account", fake_phase2), patch(
        "web.resolve_online_address",
        return_value=BillingAddress("Cluny Road", "1", "", "Singapore", "", "259569", "SG"),
    ):
        WebPayPalFlow._phase2_create_account(flow)
    schema = flow.job.runtime_schema
    assert schema["country"] == "SG"
    assert schema["currency"] == "SGD"
    assert schema["address_validation_errors"] == []
    assert schema["resolved_address"]["postalCode"] == "259569"
    assert schema["kyc"]["fields"][0]["name"] == "occupation"
    variables = flow._build_signup_variables("EC-FIXTURE")
    assert variables["country"] == "SG"
    assert variables["occupation"] == "BUSINESS"
    assert variables["phone"]["countryCode"] == "65"
    assert variables["billingAddress"]["postalCode"] == "259569"


class FakeSessionID:
    def graphql(self, operation, query, variables):
        assert operation == "GriffinMetadataQuery"
        assert variables["countryCode"] == "ID"
        return {"data": {"localeMetadata": {
            "currencyCode": "IDR",
            "address": {"layout": [
                {"name": "line1", "isRequired": True},
                {"name": "line2", "isRequired": False},
                {"name": "city", "isRequired": True},
                {"name": "state", "isRequired": True},
                {"name": "postcode", "isRequired": True, "regex": "\\d{5}"},
            ]},
            "phone": {"masks": {"mobile": "000000000000"}, "patterns": {"default": "^(0)?8\\d{8,11}$"}},
        }}}


def fake_phase2_id(self):
    self._signup_html = '{"fieldName":"identityDocumentType","isRequired":true,"fieldName2":"identityDocumentNumber","isRequired2":true,"fieldName3":"nationality","required":true}'
    self.state.ec_token = "EC-ID-FIXTURE"
    self.state.signup_url = "https://www.paypal.com/checkoutweb/signup?token=EC-ID-FIXTURE"
    self.state.content_identifier = "ID:id:fixture:compliance.signupTerms"
    return None


def test_indonesia_dynamic_kyc_fixture():
    flow = WebPayPalFlow.__new__(WebPayPalFlow)
    flow.job = FakeJob()
    flow.country = "ID"
    flow.lang = "id"
    flow.session = FakeSessionID()
    flow.user = generate_user("+628123456789", "ID")
    flow.user.email = "fixture.id@example.com"
    flow.card = CardInfo("4111111111111111", "12/2030", "123")
    flow.address = BillingAddress("", "", "", "", "", "", "ID")
    flow.state = SessionState()
    with patch.object(PayPalFlow, "_phase2_create_account", fake_phase2_id), patch(
        "web.resolve_online_address",
        return_value=BillingAddress("Jalan M.H. Thamrin", "1", "Menteng", "Jakarta Pusat", "DKI Jakarta", "10310", "ID"),
    ):
        WebPayPalFlow._phase2_create_account(flow)
    variables = flow._build_signup_variables("EC-ID-FIXTURE")
    assert variables["nationality"] == "ID"
    assert variables["identityDocument"]["type"] == "NATIONAL_ID"
    assert variables["identityDocument"]["value"] == flow.user.identity_document_number
    assert variables["billingAddress"]["postalCode"] == "10310"


if __name__ == "__main__":
    test_dynamic_country_main_flow_fixture()
    test_indonesia_dynamic_kyc_fixture()
    print("DYNAMIC_MAIN_FLOW_FIXTURE_OK")
