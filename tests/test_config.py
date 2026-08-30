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
# 4. Processor modules and display
# ===========================================================================

def test_config_processors_dict_exists():
    assert isinstance(config.processors, dict)
    assert len(config.processors) > 0


def test_config_processor_modules_is_list():
    assert isinstance(config.processor.modules, list)


def test_config_processor_modules_entries_are_strings():
    for name in config.processor.modules:
        assert isinstance(name, str) and name, f"Invalid module entry: {name!r}"


def test_config_processor_display_is_string_or_none():
    assert config.processor.display is None or isinstance(config.processor.display, str)


def test_config_processor_poll_interval_is_positive():
    assert config.processor.poll_interval_seconds > 0


def test_config_load_multiple_processors(tmp_path):
    from config import load_config
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[squawk]
data_dir = "data"

[observer]
latitude = 50.0
longitude = 0.0

[storage]
backend = "disk_drive"

[processors.pushover]
enabled = true
poll_interval_seconds = 10
modules = ["registration_filter", "adsbdb"]
display = "pushover"

[processors.screen]
enabled = false
poll_interval_seconds = 2
modules = ["closest_filter"]
display = "epaper"
""")
    loaded = load_config(cfg_file)
    assert len(loaded.processors) == 2
    assert "pushover" in loaded.processors
    assert "screen" in loaded.processors

    p_push = loaded.processors["pushover"]
    assert p_push.name == "pushover"
    assert p_push.enabled is True
    assert p_push.poll_interval_seconds == 10
    assert p_push.modules == ["registration_filter", "adsbdb"]
    assert p_push.display == "pushover"

    p_screen = loaded.processors["screen"]
    assert p_screen.name == "screen"
    assert p_screen.enabled is False
    assert p_screen.poll_interval_seconds == 2
    assert p_screen.modules == ["closest_filter"]
    assert p_screen.display == "epaper"

    # Single processor backward compatibility property returns enabled processor
    assert loaded.processor == p_push


def test_config_ignores_legacy_panel_keys(tmp_path):
    # `panel` / `panel_title` used to live on ProcessorConfig; they have been
    # replaced by [display.http.panels.<chain>] blocks. Old configs that still
    # carry the removed keys should load without raising.
    from config import load_config
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[squawk]
data_dir = "data"

[observer]
latitude = 50.0
longitude = 0.0

[storage]
backend = "disk_drive"

[processors.legacy_chain]
enabled = true
poll_interval_seconds = 5
modules = ["closest_filter"]
display = "http"
panel = "legacy_chain"
panel_title = "Legacy Panel"
""")
    loaded = load_config(cfg_file)
    p = loaded.processors["legacy_chain"]
    assert p.name == "legacy_chain"
    assert p.display == "http"
    assert not hasattr(p, "panel")
    assert not hasattr(p, "panel_title")


def test_config_load_legacy_single_processor(tmp_path):
    from config import load_config
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[squawk]
data_dir = "data"

[observer]
latitude = 50.0
longitude = 0.0

[storage]
backend = "disk_drive"

[processor]
poll_interval_seconds = 3
modules = ["closest_filter"]
display = "http"
""")
    loaded = load_config(cfg_file)
    assert len(loaded.processors) == 1
    assert "default" in loaded.processors
    p = loaded.processors["default"]
    assert p.name == "default"
    assert p.enabled is True
    assert p.poll_interval_seconds == 3
    assert p.modules == ["closest_filter"]
    assert p.display == "http"
    assert loaded.processor == p

