"""
tests/test_config.py

Tests for config.toml loading and structure.

Covers:
  1. config.toml parses without error
  2. All required sections are present
  3. Receiver URLs are syntactically valid (no network calls)
  4. Processor filters and display are configured
"""

from __future__ import annotations

import pytest

from config import config


# ===========================================================================
# 1. Load without error
# ===========================================================================

def test_config_loads_without_error():
    assert config is not None


# ===========================================================================
# 2. Required sections present
# ===========================================================================

def test_config_has_squawk_section():
    assert config.squawk is not None
    assert config.squawk.data_dir is not None


def test_config_has_observer_section():
    assert config.observer is not None
    assert isinstance(config.observer.latitude, float)
    assert isinstance(config.observer.longitude, float)


def test_config_has_ingestors_section():
    assert config.ingestors is not None


def test_config_has_storage_section():
    assert config.storage is not None
    assert isinstance(config.storage.method, str) and config.storage.method


def test_config_has_processor_section():
    assert config.processor is not None


# ===========================================================================
# 3. Receiver URLs
# ===========================================================================

def test_config_receiver_urls_are_non_empty():
    pa = config.ingestors.get("personal_adsb")
    if pa is None:
        pytest.skip("personal_adsb not configured")
    for receiver in pa.get("receivers", []):
        assert receiver["url"].startswith("http"), f"Bad URL for {receiver['name']!r}: {receiver['url']!r}"


def test_config_receiver_names_are_non_empty():
    pa = config.ingestors.get("personal_adsb")
    if pa is None:
        pytest.skip("personal_adsb not configured")
    for receiver in pa.get("receivers", []):
        assert receiver["name"], "Receiver has empty name"


# ===========================================================================
# 4. Processor plugins and display
# ===========================================================================

def test_config_processor_plugins_is_list():
    assert isinstance(config.processor.plugins, list)


def test_config_processor_plugins_entries_are_strings():
    for name in config.processor.plugins:
        assert isinstance(name, str) and name, f"Invalid plugin entry: {name!r}"


def test_config_processor_display_is_string_or_none():
    assert config.processor.display is None or isinstance(config.processor.display, str)


def test_config_processor_poll_interval_is_positive():
    assert config.processor.poll_interval_seconds > 0
