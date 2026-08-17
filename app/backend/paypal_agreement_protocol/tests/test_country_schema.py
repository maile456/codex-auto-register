from paypal.country_schema import required_address_fields, validate_address


def test_de_required_fields():
    assert required_address_fields("DE") == ("line1", "postcode", "city")
    assert validate_address("DE", {"line1": "Unter den Linden 1", "postcode": "10117", "city": "Berlin"}) == []


def test_es_state_is_required():
    errors = validate_address("ES", {"line1": "Gran Via 1", "postcode": "28013", "city": "Madrid"})
    assert "state: required" in errors


def test_ie_optional_postcode_and_state():
    assert validate_address("IE", {"line1": "1 O Connell Street", "city": "Dublin"}) == []


def test_sg_only_line1_and_postcode_required():
    assert required_address_fields("SG") == ("line1", "postcode")
    assert validate_address("SG", {"line1": "1 Raffles Place", "postcode": "048616"}) == []
