"""
tests/test_config.py

Tests for config.toml loading and structure.

Covers:
  1. config.toml parses without error
  2. All required sections are present
  3. Receiver URLs are syntactically valid (no network calls)
  4. Processor filters and display are configured
  5. Multi-processor / legacy-processor loading
  6. Structural validation — missing blocks and required keys are rejected
     with a ConfigError naming the offending block (see brief-config-strictness.md)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import ConfigError, config, load_config


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
    assert isinstance(config.storage.backend, str) and config.storage.backend


def test_config_has_processors_section():
    assert config.processors
    assert isinstance(config.processors, dict)


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
    chain = next(iter(config.processors.values()))
    assert isinstance(chain.modules, list)


def test_config_processor_modules_entries_are_strings():
    chain = next(iter(config.processors.values()))
    for name in chain.modules:
        assert isinstance(name, str) and name, f"Invalid module entry: {name!r}"


def test_config_processor_display_is_string():
    chain = next(iter(config.processors.values()))
    assert isinstance(chain.display, str) and chain.display


def test_config_processor_poll_interval_is_positive():
    chain = next(iter(config.processors.values()))
    assert chain.poll_interval_seconds > 0


# ===========================================================================
# 5. Multi-processor / legacy-processor loading
# ===========================================================================

def test_config_load_multiple_processors(tmp_path):
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

[modules.registration_filter]
[modules.adsbdb]
[modules.closest_filter]

[display.pushover]
[display.epaper]
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


def test_config_ignores_legacy_panel_keys(tmp_path):
    # `panel` / `panel_title` used to live on ProcessorConfig; they have been
    # replaced by [display.http.panels.<chain>] blocks. Old configs that still
    # carry the removed keys should load without raising.
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

[modules.closest_filter]

[display.http]
[display.http.panels.legacy_chain]
slot = 1
""")
    loaded = load_config(cfg_file)
    p = loaded.processors["legacy_chain"]
    assert p.name == "legacy_chain"
    assert p.display == "http"
    assert not hasattr(p, "panel")
    assert not hasattr(p, "panel_title")


def test_config_legacy_processor_block_produces_no_chain(tmp_path):
    # The singular [processor] syntax and SquawkConfig.processor were removed —
    # [processors.<name>] is the only supported form now.
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
    assert loaded.processors == {}
    assert not hasattr(loaded, "processor")


# ===========================================================================
# 6. Structural validation — rejections
# ===========================================================================

_BASE = """
[squawk]
data_dir = "data"

[observer]
latitude = 50.0
longitude = 0.0

[storage]
backend = "disk_drive"
"""


def _write(tmp_path: Path, body: str) -> Path:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(_BASE + body)
    return cfg_file


def test_chain_module_with_no_block_rejected(tmp_path):
    cfg_file = _write(tmp_path, """
[processors.screen]
enabled = true
modules = ["closest_filter"]
display = "console"

[display.console]
""")
    with pytest.raises(ConfigError, match="closest_filter"):
        load_config(cfg_file)


def test_chain_module_with_empty_block_accepted(tmp_path):
    cfg_file = _write(tmp_path, """
[processors.screen]
enabled = true
modules = ["closest_filter"]
display = "console"

[modules.closest_filter]

[display.console]
""")
    loaded = load_config(cfg_file)
    assert loaded.processors["screen"].modules == ["closest_filter"]


def test_chain_display_with_no_block_rejected(tmp_path):
    cfg_file = _write(tmp_path, """
[processors.screen]
enabled = true
modules = []
display = "console"
""")
    with pytest.raises(ConfigError, match="console"):
        load_config(cfg_file)


def test_chain_http_display_with_no_panel_block_rejected(tmp_path):
    cfg_file = _write(tmp_path, """
[processors.screen]
enabled = true
modules = []
display = "http"

[display.http]
port = 7700
""")
    with pytest.raises(ConfigError, match="screen"):
        load_config(cfg_file)


def test_http_panel_missing_slot_rejected(tmp_path):
    cfg_file = _write(tmp_path, """
[processors.screen]
enabled = true
modules = []
display = "http"

[display.http]
port = 7700

[display.http.panels.screen]
title = "Screen"
""")
    with pytest.raises(ConfigError, match="slot"):
        load_config(cfg_file)


@pytest.mark.parametrize("bad_slot", [0, 9, -1, 100])
def test_http_panel_slot_out_of_range_rejected(tmp_path, bad_slot):
    cfg_file = _write(tmp_path, f"""
[processors.screen]
enabled = true
modules = []
display = "http"

[display.http]
port = 7700

[display.http.panels.screen]
slot = {bad_slot}
""")
    with pytest.raises(ConfigError, match="1 to 8"):
        load_config(cfg_file)


def test_http_panel_non_integer_slot_rejected(tmp_path):
    cfg_file = _write(tmp_path, """
[processors.screen]
enabled = true
modules = []
display = "http"

[display.http]
port = 7700

[display.http.panels.screen]
slot = "1"
""")
    with pytest.raises(ConfigError, match="1 to 8"):
        load_config(cfg_file)


@pytest.mark.parametrize("good_slot", [1, 4, 5, 8])
def test_http_panel_slot_in_range_accepted(tmp_path, good_slot):
    cfg_file = _write(tmp_path, f"""
[processors.screen]
enabled = true
modules = []
display = "http"

[display.http]
port = 7700

[display.http.panels.screen]
slot = {good_slot}
""")
    assert load_config(cfg_file) is not None


def test_http_panel_duplicate_slots_rejected_naming_both_chains(tmp_path):
    cfg_file = _write(tmp_path, """
[processors.alpha]
enabled = true
modules = []
display = "http"

[processors.beta]
enabled = true
modules = []
display = "http"

[display.http]
port = 7700

[display.http.panels.alpha]
slot = 3

[display.http.panels.beta]
slot = 3
""")
    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_file)
    message = str(exc_info.value)
    assert "alpha" in message
    assert "beta" in message
    assert "3" in message


def test_http_panel_order_key_rejected_with_message_naming_slot(tmp_path):
    # config.toml is gitignored, so it survives every code change that
    # invalidates it. Say which key replaced 'order' rather than only that
    # 'slot' is missing.
    cfg_file = _write(tmp_path, """
[processors.screen]
enabled = true
modules = []
display = "http"

[display.http]
port = 7700

[display.http.panels.screen]
title = "Screen"
order = 1
""")
    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_file)
    message = str(exc_info.value)
    assert "order" in message
    assert "slot" in message


def _list_panel(tmp_path, panel_body: str):
    return _write(tmp_path, f"""
[processors.panel_one]
enabled = true
modules = []
display = "http"

[display.http]
port = 7700

[display.http.panels.panel_one]
slot = 1
{panel_body}
""")


def test_http_panel_list_layout_with_bands_accepted(tmp_path):
    cfg_file = _list_panel(tmp_path, 'layout = "list"\nbands  = ["D", "C", "B", "A"]')
    loaded = load_config(cfg_file)
    panel = loaded.display["http"]["panels"]["panel_one"]
    assert panel["layout"] == "list"
    assert panel["bands"] == ["D", "C", "B", "A"]


def test_http_panel_list_layout_without_bands_rejected(tmp_path):
    cfg_file = _list_panel(tmp_path, 'layout = "list"')
    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_file)
    message = str(exc_info.value)
    assert "panel_one" in message
    assert "bands" in message


def test_http_panel_list_layout_with_empty_bands_rejected(tmp_path):
    cfg_file = _list_panel(tmp_path, 'layout = "list"\nbands  = []')
    with pytest.raises(ConfigError, match="bands"):
        load_config(cfg_file)


def test_http_panel_bands_without_list_layout_rejected_naming_both_keys(tmp_path):
    cfg_file = _list_panel(tmp_path, 'layout = "card"\nbands  = ["D", "C"]')
    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_file)
    message = str(exc_info.value)
    assert "bands" in message
    assert "layout" in message


def test_http_panel_bands_with_default_layout_rejected(tmp_path):
    # No 'layout' key at all is the card default, so this is the same mistake.
    cfg_file = _list_panel(tmp_path, 'bands = ["D", "C"]')
    with pytest.raises(ConfigError, match="bands"):
        load_config(cfg_file)


def test_http_panel_duplicate_band_rejected(tmp_path):
    cfg_file = _list_panel(tmp_path, 'layout = "list"\nbands  = ["D", "C", "C", "A"]')
    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_file)
    message = str(exc_info.value)
    assert "panel_one" in message
    assert "'C'" in message


@pytest.mark.parametrize("bad_band", ['"AB"', '"1"', '"c"', '""', "3"])
def test_http_panel_invalid_band_entry_rejected(tmp_path, bad_band):
    cfg_file = _list_panel(tmp_path, f'layout = "list"\nbands  = ["D", {bad_band}]')
    with pytest.raises(ConfigError, match="A to Z"):
        load_config(cfg_file)


def test_http_panel_unknown_layout_rejected_listing_valid_values(tmp_path):
    cfg_file = _list_panel(tmp_path, 'layout = "grid"')
    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_file)
    message = str(exc_info.value)
    assert "grid" in message
    assert "card" in message
    assert "list" in message


def test_http_panel_without_layout_or_bands_still_validates(tmp_path):
    # Every existing panel block keeps working untouched.
    cfg_file = _list_panel(tmp_path, 'title = "Plain"')
    loaded = load_config(cfg_file)
    assert "layout" not in loaded.display["http"]["panels"]["panel_one"]


def test_http_eight_chains_in_eight_slots_accepted(tmp_path):
    body = ""
    for i in range(1, 9):
        body += f"""
[processors.chain_{i}]
enabled = true
modules = []
display = "http"
"""
    body += '\n[display.http]\nport = 7700\n'
    for i in range(1, 9):
        body += f"""
[display.http.panels.chain_{i}]
title = "Chain {i}"
slot = {i}
"""
    loaded = load_config(_write(tmp_path, body))
    assert len(loaded.processors) == 8
    panels = loaded.display["http"]["panels"]
    assert sorted(p["slot"] for p in panels.values()) == list(range(1, 9))


def test_http_panel_slot_check_ignores_non_http_chains(tmp_path):
    # A chain on another display must not be dragged into slot validation.
    cfg_file = _write(tmp_path, """
[processors.screen]
enabled = true
modules = []
display = "console"

[display.console]
""")
    assert load_config(cfg_file) is not None


def test_chain_missing_enabled_rejected(tmp_path):
    cfg_file = _write(tmp_path, """
[processors.screen]
modules = []
display = "console"

[display.console]
""")
    with pytest.raises(ConfigError, match="enabled"):
        load_config(cfg_file)


def test_chain_missing_modules_rejected(tmp_path):
    cfg_file = _write(tmp_path, """
[processors.screen]
enabled = true
display = "console"

[display.console]
""")
    with pytest.raises(ConfigError, match="modules"):
        load_config(cfg_file)


def test_chain_missing_display_rejected(tmp_path):
    cfg_file = _write(tmp_path, """
[processors.screen]
enabled = true
modules = []
""")
    with pytest.raises(ConfigError, match="display"):
        load_config(cfg_file)


def test_chain_empty_modules_with_valid_display_accepted(tmp_path):
    cfg_file = _write(tmp_path, """
[processors.screen]
enabled = true
modules = []
display = "console"

[display.console]
""")
    loaded = load_config(cfg_file)
    assert loaded.processors["screen"].modules == []


def test_ingestor_missing_enabled_rejected(tmp_path):
    cfg_file = _write(tmp_path, """
[ingestors.concorde]
""")
    with pytest.raises(ConfigError, match="concorde"):
        load_config(cfg_file)


def test_personal_adsb_missing_receivers_rejected(tmp_path):
    cfg_file = _write(tmp_path, """
[ingestors.personal_adsb]
enabled = true
""")
    with pytest.raises(ConfigError, match="receivers"):
        load_config(cfg_file)


def test_malformed_toml_rejected_with_clean_message(tmp_path):
    # An invalid TOML value (bareword, not a quoted string/number/bool) must
    # surface as our own ConfigError, not a raw tomllib.TOMLDecodeError with
    # its internal parser traceback.
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[modules.adsbdb]
Zippy=NO
""")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(cfg_file)


def test_missing_squawk_section_rejected(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[observer]
latitude = 50.0
longitude = 0.0

[storage]
backend = "disk_drive"
""")
    with pytest.raises(ConfigError, match=r"\[squawk\]"):
        load_config(cfg_file)


def test_missing_storage_section_rejected(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[squawk]
data_dir = "data"

[observer]
latitude = 50.0
longitude = 0.0
""")
    with pytest.raises(ConfigError, match=r"\[storage\]"):
        load_config(cfg_file)


def test_missing_observer_section_rejected(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[squawk]
data_dir = "data"

[storage]
backend = "disk_drive"
""")
    with pytest.raises(ConfigError, match=r"\[observer\]"):
        load_config(cfg_file)


def test_multiple_problems_reported_together(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("""
[storage]
backend = "disk_drive"

[ingestors.concorde]
""")
    with pytest.raises(ConfigError) as exc_info:
        load_config(cfg_file)
    message = str(exc_info.value)
    assert "observer" in message
    assert "concorde" in message


def test_unreferenced_module_block_warns_but_loads(tmp_path, capsys):
    cfg_file = _write(tmp_path, """
[modules.orphan_filter]
""")
    loaded = load_config(cfg_file)
    assert loaded is not None
    captured = capsys.readouterr()
    assert "orphan_filter" in captured.out
    assert "not referenced" in captured.out


def test_config_toml_example_loads_without_error():
    example_path = Path(__file__).parent.parent / "config.toml.example"
    loaded = load_config(example_path)
    assert loaded is not None


def test_removed_ground_distance_synonym_no_longer_applies():
    # Config-level companion to modules/ground_distance_filter's own test:
    # a stale 'within' key must not silently set a maximum any more.
    from config import ObserverConfig
    from modules import ModuleContext
    from modules.ground_distance_filter import get

    ctx = ModuleContext(
        data_dir=Path("."),
        module_dir=Path("./modules/ground_distance_filter"),
        observer=ObserverConfig(latitude=0.0, longitude=0.0),
    )
    gdf = get({"within": 10}, ctx)
    assert gdf._max_distance_nm is None


def test_unknown_key_warning_fires_for_module_declaring_keys(capsys):
    from modules import get_module
    get_module("ground_distance_filter", {"max_distance": 10, "belwo": 5})
    captured = capsys.readouterr()
    assert "belwo" in captured.out
    assert "ground_distance_filter" in captured.out


def test_unknown_key_warning_does_not_fire_for_module_without_keys(capsys, monkeypatch):
    # All eight shipping modules declare KEYS, so simulate one that doesn't.
    import modules.pass_through as pass_through_module
    monkeypatch.delattr(pass_through_module, "KEYS", raising=False)

    from modules import get_module
    get_module("pass_through", {"anything": 1})
    captured = capsys.readouterr()
    assert captured.out == ""


def test_every_module_declares_keys():
    import importlib, pkgutil, modules
    for info in pkgutil.iter_modules(modules.__path__):
        m = importlib.import_module(f"modules.{info.name}")
        assert hasattr(m, "KEYS"), f"modules/{info.name}.py has no KEYS"
        assert "type" in m.KEYS
