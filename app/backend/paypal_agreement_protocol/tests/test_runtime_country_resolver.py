from paypal.runtime_country_resolver import resolve_runtime_country_schema


class FakeSession:
    def graphql(self, operation, query, variables):
        assert operation == "GriffinMetadataQuery"
        assert variables["countryCode"] == "SG"
        return {"data": {"localeMetadata": {
            "currencyCode": "SGD",
            "address": {"layout": [
                {"name": "line1", "isRequired": True, "maxLength": 100, "minLength": None, "regex": None},
                {"name": "line2", "isRequired": False, "maxLength": 100, "minLength": None, "regex": None},
                {"name": "postcode", "isRequired": True, "maxLength": 6, "minLength": 6, "regex": "\\d{6}"},
            ]},
            "phone": {"masks": {"mobile": "0000 0000"}, "patterns": {"default": "[3689]\\d{7}"}},
        }}}


def test_runtime_schema_normalization():
    schema = resolve_runtime_country_schema(FakeSession(), "sg")
    assert schema["country"] == "SG"
    assert schema["currency"] == "SGD"
    assert [x["paypal_name"] for x in schema["address_fields"]] == ["line1", "line2", "postcode"]
    assert schema["phone_mask"] == "0000 0000"
