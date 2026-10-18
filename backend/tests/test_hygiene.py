"""The suite itself: no real .env, no credentials, no network, no real Gemini client."""

import socket

import pytest

from app.ai import gemini
from app.config import ENV_FILES, Settings, get_settings


def test_settings_never_read_the_repo_env_file():
    assert ENV_FILES is None and Settings.model_config["env_file"] is None
    s = get_settings()
    assert s.gemini_api_key == "" and s.paypal_client_id == "" and s.paypal_client_secret == ""
    assert "checkout_guard_test" in s.database_url


def test_outbound_connections_are_refused_and_reported(no_network):
    leaks = no_network
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="must not use the network"):
            sock.connect(("203.0.113.7", 443))  # TEST-NET-3: never routable
    finally:
        sock.close()
    assert leaks == ["outbound connection to ('203.0.113.7', 443)"]
    leaks.clear()  # expected here; any other test that leaks fails at teardown


def test_real_gemini_client_is_unreachable(no_network):
    with pytest.raises(RuntimeError, match="unmocked Gemini"):
        gemini._client()
    assert no_network == ["real Gemini client requested"]
    no_network.clear()
