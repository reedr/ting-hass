"""Fixtures for Ting integration tests."""

from unittest.mock import patch

import pytest

pytest_plugins = ("pytest_homeassistant_custom_component",)


@pytest.fixture(autouse=True)
def enable_ting_integration(enable_custom_integrations):
    """Allow Home Assistant to load the custom integration under test."""


@pytest.fixture(autouse=True)
def no_ting_notifications():
    """Tests that set up the entry see an empty alert history unless they patch it."""
    with patch("custom_components.ting.TingApi.async_get_notifications", return_value=[]):
        yield
