# A1: BlockSignalDetector tests — one per fixture class, plus a clean-200 negative.

from src.domain.block_signal import detect_block


def test_block_status_codes_fire():
    for code in (403, 429, 503):
        assert detect_block(status_code=code).blocked


def test_challenge_title_fires():
    signal = detect_block(status_code=200, title="Just a moment...")
    assert signal.blocked
    assert "title" in signal.reason


def test_turnstile_body_marker_fires():
    body = '<html><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></html>'
    signal = detect_block(status_code=200, title="Example", body_sample=body)
    assert signal.blocked


def test_ctype_managed_marker_fires():
    body = '{"cType":"managed","cRay":"abc123"}'
    signal = detect_block(status_code=200, body_sample=body)
    assert signal.blocked


def test_clean_200_is_not_blocked():
    signal = detect_block(status_code=200, title="Example Domain", body_sample="<html>hello</html>")
    assert not signal.blocked
    assert signal.reason is None
