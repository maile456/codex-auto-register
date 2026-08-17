from paypal.flow import PayPalFlow
from paypal.models import CardInfo, SessionState, generate_address, generate_user


def test_sg_runtime_pipeline_variables():
    flow = PayPalFlow.__new__(PayPalFlow)
    flow.country = "SG"
    flow.lang = "en"
    flow.user = generate_user("+6581234567", "SG")
    flow.user.email = "fixture@example.com"
    flow.card = CardInfo("4111111111111111", "12/2030", "123")
    flow.address = generate_address("SG")
    flow.address.street = "Cluny Road"
    flow.address.house_number = "1"
    flow.address.city = "Singapore"
    flow.address.postal_code = "259569"
    flow.state = SessionState(content_identifier="SG:en:fixture:compliance.signupTerms")
    flow.runtime_form_schema = {
        "address_fields": [
            {"paypal_name": "line1", "required": True},
            {"paypal_name": "postcode", "required": True, "pattern": r"\d{6}"},
        ],
        "kyc": {"fields": []},
    }
    variables = flow._build_signup_variables("EC-FIXTURE")
    assert variables["country"] == "SG"
    assert variables["phone"]["countryCode"] == "65"
    assert variables["billingAddress"]["line1"] == "Cluny Road, 1"
    assert variables["billingAddress"]["postalCode"] == "259569"
    assert variables["contentIdentifier"].startswith("SG:en:")


if __name__ == "__main__":
    test_sg_runtime_pipeline_variables()
    print("DYNAMIC_COUNTRY_PIPELINE_TEST_OK")
