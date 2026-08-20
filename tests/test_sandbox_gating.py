# Regression tests for SEC-5: --no-sandbox must not be unconditional.
# Off by default (sandbox stays enabled); on only when a container is
# detected or SCRAPER_DISABLE_SANDBOX explicitly says so.

from src.infrastructure.browser.pool import (
    _running_in_container,
    _sandbox_should_be_disabled,
)


def test_defaults_to_sandbox_enabled_outside_a_container(monkeypatch):
    monkeypatch.delenv("SCRAPER_DISABLE_SANDBOX", raising=False)
    monkeypatch.setattr(
        "src.infrastructure.browser.pool._running_in_container", lambda: False
    )
    assert _sandbox_should_be_disabled() is False


def test_disables_sandbox_when_container_detected(monkeypatch):
    monkeypatch.delenv("SCRAPER_DISABLE_SANDBOX", raising=False)
    monkeypatch.setattr(
        "src.infrastructure.browser.pool._running_in_container", lambda: True
    )
    assert _sandbox_should_be_disabled() is True


def test_explicit_true_override_wins_even_outside_a_container(monkeypatch):
    monkeypatch.setenv("SCRAPER_DISABLE_SANDBOX", "true")
    monkeypatch.setattr(
        "src.infrastructure.browser.pool._running_in_container", lambda: False
    )
    assert _sandbox_should_be_disabled() is True


def test_explicit_false_override_wins_even_inside_a_container(monkeypatch):
    monkeypatch.setenv("SCRAPER_DISABLE_SANDBOX", "false")
    monkeypatch.setattr(
        "src.infrastructure.browser.pool._running_in_container", lambda: True
    )
    assert _sandbox_should_be_disabled() is False


def test_dockerenv_file_marks_container(monkeypatch):
    monkeypatch.setattr("src.infrastructure.browser.pool.os.path.exists", lambda p: True)
    assert _running_in_container() is True


def test_no_dockerenv_and_no_cgroup_file_is_not_a_container(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr("src.infrastructure.browser.pool.os.path.exists", lambda p: False)
    monkeypatch.setattr("builtins.open", _raise)
    assert _running_in_container() is False
