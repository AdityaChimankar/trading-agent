"""Offline checks for the optional manual login-form diagnostic."""
from unittest.mock import Mock, patch
from types import SimpleNamespace

import pytest

from trading_agent.scripts import inspect_kite_login


def test_input_description_never_reads_values_or_page_contents():
    element = Mock()
    element.get_attribute.side_effect = lambda attribute: {
        "type": "password", "name": "password", "id": "password",
    }[attribute]
    page = Mock()
    page.query_selector_all.return_value = [element]
    assert inspect_kite_login.describe_inputs(page) == [
        {"type": "password", "name": "password", "id": "password"},
    ]
    assert [call.args[0] for call in element.get_attribute.call_args_list] == ["type", "name", "id"]
    page.content.assert_not_called()


def test_help_does_not_load_credentials_or_start_a_browser(capsys):
    with patch("trading_agent.core.config.get_settings") as settings:
        with pytest.raises(SystemExit) as result:
            inspect_kite_login.main(["--help"])
        assert result.value.code == 0
        settings.assert_not_called()
    assert "--submit-credentials" in capsys.readouterr().out


@pytest.mark.parametrize("submit", [False, True])
def test_credential_submission_requires_explicit_flag(submit, capsys):
    settings = SimpleNamespace(kite=SimpleNamespace(
        api_key="dummy-key", user_id="dummy-user", password="dummy-password",
    ))
    with patch("trading_agent.core.config.get_settings", return_value=settings), \
         patch("kiteconnect.KiteConnect") as kite, \
         patch("playwright.sync_api.sync_playwright") as playwright:
        kite.return_value.login_url.return_value = "https://example.invalid/login"
        browser = playwright.return_value.__enter__.return_value.chromium.launch.return_value
        page = browser.new_page.return_value
        page.query_selector_all.return_value = []
        assert inspect_kite_login.main(["--submit-credentials"] if submit else []) == 0
        assert page.fill.call_count == (2 if submit else 0)
        assert page.click.call_count == (1 if submit else 0)
        browser.close.assert_called_once()
    output = capsys.readouterr().out
    for secret in ("dummy-key", "dummy-user", "dummy-password"):
        assert secret not in output
