"""
tests/test_data_sources.py

Tests for the shared data-source infrastructure.

Covers:
  1. Factory — get_data_source() pools by (name, cfg), same as get_module()
  2. Wiring — ctx.data_source(name) resolves a source, and two modules naming
     the same one get the same instance
  3. ensure_fresh() — the contract concrete types must honour
  4. directory — under <data_dir>/data_sources/<name>/, never a module_dir

Config-level loading and cross-checks are in test_config.py, section 7.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import data_sources
import modules
from config import DataSourceConfig, ObserverConfig
from data_sources import (
    BaseDataSource, DataSourceContext, clear_data_source_pool, get_data_source,
)
from modules import ModuleContext, clear_module_pool, get_module


# ---------------------------------------------------------------------------
# Keep both factory pools isolated between tests in this file
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_pools():
    clear_data_source_pool()
    clear_module_pool()
    yield
    clear_data_source_pool()
    clear_module_pool()


# ---------------------------------------------------------------------------
# Fakes
#
# modules/ has pass_through — a real, shipping, do-nothing module that the
# factory tests lean on instead of fabricating one. There is deliberately no
# equivalent here: a source whose "download" is a counter, or a module that
# exists only to record what its ctx handed it, has no use outside these
# tests. They are built here and registered under the package import path the
# factory resolves against, rather than shipped as dead production code.
# ---------------------------------------------------------------------------

class FakeSource(BaseDataSource):
    """Counts fetches instead of downloading, on a configurable stale window."""

    def __init__(self, cfg: dict, ctx: DataSourceContext) -> None:
        self.cfg = cfg
        self.ctx = ctx
        self.fetches = 0
        self.now = 0.0                                     # test-driven clock
        self._window = cfg.get("stale_after_seconds", 3600)
        self._last_fetch: float | None = None

    def ensure_fresh(self) -> None:
        if self._last_fetch is None or self.now - self._last_fetch >= self._window:
            self.fetches += 1
            self._last_fetch = self.now

    @property
    def directory(self) -> Path:
        return self.ctx.source_dir


@pytest.fixture
def fake_source_type(monkeypatch):
    """Register data_sources.fake_source so cfg type = "fake_source" resolves."""
    module = types.ModuleType("data_sources.fake_source")
    module.KEYS = {"stale_after_seconds"}
    module.get = lambda cfg, ctx: FakeSource(cfg, ctx)
    monkeypatch.setitem(sys.modules, "data_sources.fake_source", module)
    return module


@pytest.fixture
def fake_module_type(monkeypatch):
    """Register modules.fake_consumer — a module that resolves its source.

    Exactly what a real consumer's get() will do: read cfg["source"], hand the
    name to ctx.data_source, keep the instance.
    """
    module = types.ModuleType("modules.fake_consumer")
    module.KEYS = {"type"}

    class FakeConsumer(modules.BaseModule):
        def __init__(self, source):
            self.source = source

        def process(self, aircraft):
            return aircraft

    module.get = lambda cfg, ctx: FakeConsumer(ctx.data_source(cfg["source"]))
    monkeypatch.setitem(sys.modules, "modules.fake_consumer", module)
    return module


@pytest.fixture
def installed_config(monkeypatch, tmp_path, fake_source_type):
    """Point global config at tmp_path with one [data_sources.vrs] block."""
    from config import config as squawk_config

    monkeypatch.setattr(squawk_config.squawk, "data_dir", str(tmp_path))
    monkeypatch.setattr(squawk_config, "data_sources", {
        "vrs": DataSourceConfig(
            name="vrs", type="fake_source", cfg={"type": "fake_source"},
        ),
    })
    return squawk_config


def _ctx(tmp_path: Path) -> ModuleContext:
    return ModuleContext(
        data_dir=tmp_path,
        module_dir=tmp_path / "modules" / "fake_consumer",
        observer=ObserverConfig(latitude=53.7778, longitude=-1.5721),
    )


# ===========================================================================
# 1. Factory — pooling by (name, cfg)
#
# Config-level loading and cross-checks live in test_config.py, beside the
# [modules.*] and [ingestors.*] checks they mirror.
# ===========================================================================

def test_same_name_same_config_one_instance(installed_config):
    cfg = {"type": "fake_source"}
    assert get_data_source("vrs", cfg) is get_data_source("vrs", cfg)


def test_same_type_different_blocks_are_distinct_instances(installed_config):
    # Config is part of the pool key, same as get_module(): two blocks of one
    # type are two datasets, and must not collapse into one instance.
    a = get_data_source("vrs_uk", {"type": "fake_source", "stale_after_seconds": 60})
    b = get_data_source("vrs_eu", {"type": "fake_source", "stale_after_seconds": 900})
    assert a is not b
    assert a.cfg["stale_after_seconds"] == 60
    assert b.cfg["stale_after_seconds"] == 900


def test_same_name_different_config_are_distinct_instances(installed_config):
    a = get_data_source("vrs", {"type": "fake_source", "stale_after_seconds": 60})
    b = get_data_source("vrs", {"type": "fake_source", "stale_after_seconds": 61})
    assert a is not b


def test_type_defaults_to_the_block_name(installed_config):
    # No 'type' key: the block name is the type, same fallback get_module uses.
    assert isinstance(get_data_source("fake_source"), FakeSource)


def test_unknown_source_raises_and_leaves_no_pool_entry(installed_config):
    with pytest.raises(ValueError, match="nonexistent"):
        get_data_source("nonexistent")
    assert not any(key[0] == "nonexistent" for key in data_sources._INSTANCES)


def test_unknown_key_warning_names_the_source_block(installed_config, capsys):
    get_data_source("vrs", {"type": "fake_source", "stale_after_secnods": 60})
    captured = capsys.readouterr()
    assert "stale_after_secnods" in captured.out
    assert "vrs" in captured.out


def test_clear_data_source_pool_produces_fresh_instances(installed_config):
    first = get_data_source("vrs", {"type": "fake_source"})
    clear_data_source_pool()
    assert get_data_source("vrs", {"type": "fake_source"}) is not first


# ===========================================================================
# 2. Wiring — a module resolves its source through ctx
# ===========================================================================

def test_two_modules_naming_one_source_share_one_instance(
    installed_config, fake_module_type,
):
    # The point of the whole exercise: one download, however many modules.
    route = get_module("vrs_route",    {"type": "fake_consumer", "source": "vrs"})
    craft = get_module("vrs_aircraft", {"type": "fake_consumer", "source": "vrs"})
    assert route is not craft
    assert route.source is craft.source


def test_module_resolving_an_unconfigured_source_raises(installed_config, fake_module_type):
    with pytest.raises(ValueError, match="absent"):
        get_module("stray", {"type": "fake_consumer", "source": "absent"})


def test_source_key_does_not_trigger_the_unknown_key_warning(installed_config, capsys):
    # 'source' is recognised on any module block, so no module lists it in its
    # own KEYS — pass_through declares only {"type"}.
    get_module("pass_through", {"source": "vrs"})
    assert capsys.readouterr().out == ""


def test_context_built_by_hand_still_resolves_configured_sources(installed_config, tmp_path):
    # The resolver is ModuleContext's default, so the 3-field contexts tests
    # and display/ build keep working — and keep resolving.
    assert isinstance(_ctx(tmp_path).data_source("vrs"), FakeSource)


# ===========================================================================
# 3. ensure_fresh() — idempotent, cheap to call often
# ===========================================================================

def test_ensure_fresh_fetches_once_then_holds(installed_config):
    source = get_data_source("vrs", {"type": "fake_source", "stale_after_seconds": 3600})
    for _ in range(10):
        source.ensure_fresh()
    assert source.fetches == 1


def test_ensure_fresh_fetches_again_once_the_window_elapses(installed_config):
    source = get_data_source("vrs", {"type": "fake_source", "stale_after_seconds": 3600})
    source.ensure_fresh()
    source.now += 3599
    source.ensure_fresh()
    assert source.fetches == 1

    source.now += 1
    source.ensure_fresh()
    assert source.fetches == 2


def test_ensure_fresh_is_shared_state_across_the_modules_using_it(
    installed_config, fake_module_type,
):
    # Two modules, one source: the second module's call sees the first's fetch
    # rather than starting its own.
    route = get_module("vrs_route",    {"type": "fake_consumer", "source": "vrs"})
    craft = get_module("vrs_aircraft", {"type": "fake_consumer", "source": "vrs"})
    route.source.ensure_fresh()
    craft.source.ensure_fresh()
    assert route.source.fetches == 1


# ===========================================================================
# 4. directory — a source's own, distinct from any module's
# ===========================================================================

def test_directory_sits_under_data_sources_keyed_on_block_name(installed_config, tmp_path):
    source = get_data_source("vrs", {"type": "fake_source"})
    assert source.directory == tmp_path / "data_sources" / "vrs"


def test_directory_is_not_a_module_dir(installed_config, tmp_path):
    source = get_data_source("vrs", {"type": "fake_source"})
    assert source.directory != _ctx(tmp_path).module_dir
    assert "modules" not in source.directory.parts


def test_two_blocks_of_one_type_get_separate_directories(installed_config):
    # Keyed on block name, not type (module_dir is the other way round): two
    # datasets of the same type must not overwrite each other's files.
    uk = get_data_source("vrs_uk", {"type": "fake_source"})
    eu = get_data_source("vrs_eu", {"type": "fake_source"})
    assert uk.directory != eu.directory
    assert uk.directory.name == "vrs_uk"
    assert eu.directory.name == "vrs_eu"


def test_factory_does_not_create_the_source_directory(installed_config, tmp_path):
    source = get_data_source("vrs", {"type": "fake_source"})
    assert not source.directory.exists()
