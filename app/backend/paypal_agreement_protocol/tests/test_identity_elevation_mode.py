import unittest

from paypal.elevation_flow import IdentityElevationPayPalFlow
from paypal.models import SessionState


class FakeSession:
    def __init__(self, checkout_type="BILLING_WITHOUT_PURCHASE"):
        self.checkout_type = checkout_type
        self.calls = []

    def graphql(self, operation_name, query, variables, **kwargs):
        self.calls.append((operation_name, variables, kwargs))
        return {
            "data": {
                "checkoutSession": {
                    "checkoutSessionType": self.checkout_type,
                }
            }
        }


class IdentityElevationModeTests(unittest.TestCase):
    def test_checkout_context_accepts_billing_without_purchase(self):
        checkout = IdentityElevationPayPalFlow._require_checkout_session({
            "data": {
                "checkoutSession": {
                    "checkoutSessionType": "BILLING_WITHOUT_PURCHASE",
                }
            }
        })
        self.assertEqual(checkout["checkoutSessionType"], "BILLING_WITHOUT_PURCHASE")

    def test_checkout_context_rejects_other_type(self):
        with self.assertRaisesRegex(RuntimeError, "TYPE_MISMATCH"):
            IdentityElevationPayPalFlow._require_checkout_session({
                "data": {"checkoutSession": {"checkoutSessionType": "ONE_TIME_PURCHASE"}}
            })

    def test_protocol_elevation_requires_ec(self):
        flow = IdentityElevationPayPalFlow.__new__(IdentityElevationPayPalFlow)
        flow.state = SessionState(euat_token="EUAT")
        with self.assertRaisesRegex(RuntimeError, "EC_MISSING"):
            flow._protocol_identity_elevation()

    def test_protocol_elevation_requires_validated_signup_context(self):
        flow = IdentityElevationPayPalFlow.__new__(IdentityElevationPayPalFlow)
        flow.state = SessionState(
            ec_token="EC-ABC123456789",  # gitleaks:allow
            euat_token="EUAT",
            signup_context_ready=False,
        )
        with self.assertRaisesRegex(RuntimeError, "SIGNUP_CONTEXT_NOT_READY"):
            flow._protocol_identity_elevation()


if __name__ == "__main__":
    unittest.main()
