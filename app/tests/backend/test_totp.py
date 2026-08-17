from __future__ import annotations

import pytest

from backend.totp import TotpSecretError, generate_totp, normalize_totp_secret


def test_generate_totp_matches_rfc_6238_sha1_vector() -> None:
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # gitleaks:allow
    assert generate_totp(secret, timestamp=59, digits=8) == "94287082"


def test_totp_secret_normalization_and_validation() -> None:
    assert normalize_totp_secret("jbsw y3dp ehpk 3pxp") == "JBSWY3DPEHPK3PXP"
    with pytest.raises(TotpSecretError):
        normalize_totp_secret("not-base32!")
