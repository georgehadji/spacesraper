from src.domain.exceptions import SSRFGuardError, InputValidationError, SpacescraperError

def test_ssrf_guard_error_is_spacescraper_error():
    err = SSRFGuardError("blocked", code="SSRF_BLOCKED")
    assert isinstance(err, SpacescraperError)
    assert err.code == "SSRF_BLOCKED"

def test_input_validation_error_is_spacescraper_error():
    err = InputValidationError("too large", code="PAYLOAD_TOO_LARGE")
    assert isinstance(err, SpacescraperError)
    assert err.code == "PAYLOAD_TOO_LARGE"
