"""
tests/test_config.py

Config validation.

The rule Squawk follows is that structure is strict and tuning is
lenient: a mistyped module name is a mistake and must stop the program,
where an unset poll interval is a preference and may default. These tests
pin both halves of that, because "fails loudly at startup" is only true
until someone adds a well-meaning `.get(key, fallback)`.
"""

from __future__ import annotations

import textwrap

import pytest

from config import ConfigError, build_config, load_config

MINIMAL = """
observer_latitude  = 52.0
observer_longitude = -1.0

[snapshot_chain]
type = "file_object"
transform = ["nop"]

[ingest_chain.concorde_A]
type = "ingest_concorde"
transform = ["nop"]

[output_chain.display]
type = "output_console"
transform = ["nop"]
"""


def parse(toml_text: str):
    import tomllib
    return build_config(tomllib.loads(textwrap.dedent(toml_text)))


def test_a_minimal_config_loads(tmp_path):
    config = parse(MINIMAL)

    assert config.observer_latitude == 52.0
    assert list(config.ingest_chains) == ["concorde_A"]
    assert list(config.output_chains) == ["display"]
    assert config.snapshot_chain.type == "file_object"


def test_data_dir_defaults_to_data():
    assert parse(MINIMAL).data_dir.name == "data"


def test_tuning_knobs_default():
    config = parse(MINIMAL)

    assert config.snapshot_chain.expiry_minutes == 5.0
    assert config.output_chains["display"].poll_interval_seconds == 5.0
    assert config.ingest_chains["concorde_A"].debug_level == "warn"


def test_the_example_config_is_valid(tmp_path):
    """config.toml.example is the reference users copy — it must load."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "config.toml.example"
    config  = load_config(example)

    assert sorted(config.ingest_chains) == ["concorde_A", "concorde_B"]
    assert sorted(config.output_chains) == ["display", "history"]


# ---------------------------------------------------------------------------
# Structural errors
# ---------------------------------------------------------------------------

def test_a_missing_snapshot_chain_is_an_error():
    with pytest.raises(ConfigError, match="no \\[snapshot_chain\\] block"):
        parse(MINIMAL.replace('[snapshot_chain]\ntype = "file_object"',
                              '[unused]\ntype = "file_object"'))


def test_a_second_snapshot_chain_is_an_error():
    with pytest.raises(ConfigError, match="exactly one snapshot chain"):
        parse("""
            observer_latitude  = 52.0
            observer_longitude = -1.0

            [snapshot_chain.primary]
            type = "file_object"

            [snapshot_chain.secondary]
            type = "file_object"

            [ingest_chain.concorde_A]
            type = "ingest_concorde"

            [output_chain.display]
            type = "output_console"
        """)


def test_a_repeated_snapshot_chain_header_is_rejected_by_toml(tmp_path):
    """The other way of writing two — TOML itself catches this one."""
    path = tmp_path / "config.toml"
    path.write_text(MINIMAL + '\n[snapshot_chain]\ntype = "file_object"\n')

    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(path)


def test_an_unknown_chain_type_is_an_error():
    with pytest.raises(ConfigError, match="unknown type 'ingest_gibberish'"):
        parse(MINIMAL.replace('type = "ingest_concorde"', 'type = "ingest_gibberish"'))


def test_an_unknown_snapshot_type_is_an_error():
    with pytest.raises(ConfigError, match="unknown type 'sqlite'"):
        parse(MINIMAL.replace('type = "file_object"', 'type = "sqlite"'))


def test_an_unknown_output_type_is_an_error():
    with pytest.raises(ConfigError, match="unknown type 'output_hologram'"):
        parse(MINIMAL.replace('type = "output_console"', 'type = "output_hologram"'))


def test_an_unknown_transform_is_an_error():
    with pytest.raises(ConfigError, match="unknown transform 'enhance'"):
        parse(MINIMAL.replace('transform = ["nop"]', 'transform = ["nop", "enhance"]', 1))


def test_an_unknown_top_level_key_is_an_error():
    with pytest.raises(ConfigError, match="unknown top-level key 'port'"):
        parse("port = 8080\n" + MINIMAL)


def test_an_unknown_key_inside_a_chain_is_an_error():
    with pytest.raises(ConfigError, match="unknown key 'colour'"):
        parse(MINIMAL.replace('[output_chain.display]\ntype = "output_console"',
                              '[output_chain.display]\ncolour = "green"\ntype = "output_console"'))


def test_a_missing_type_is_an_error():
    with pytest.raises(ConfigError, match="missing 'type'"):
        parse(MINIMAL.replace('[ingest_chain.concorde_A]\ntype = "ingest_concorde"',
                              '[ingest_chain.concorde_A]'))


def test_a_missing_observer_position_is_an_error():
    with pytest.raises(ConfigError, match="missing 'observer_latitude'"):
        parse(MINIMAL.replace("observer_latitude  = 52.0", ""))


def test_an_unknown_debug_level_is_an_error():
    with pytest.raises(ConfigError, match="debug_level 'verbose'"):
        parse(MINIMAL + '\n[output_chain.other]\ntype = "output_console"\ndebug_level = "verbose"\n')


def test_no_ingest_chains_is_an_error():
    with pytest.raises(ConfigError, match="no \\[ingest_chain"):
        parse(MINIMAL.replace('[ingest_chain.concorde_A]\ntype = "ingest_concorde"\ntransform = ["nop"]', ""))


def test_no_output_chains_is_an_error():
    with pytest.raises(ConfigError, match="no \\[output_chain"):
        parse(MINIMAL.replace('[output_chain.display]\ntype = "output_console"\ntransform = ["nop"]', ""))


def test_every_problem_is_reported_at_once():
    """One run, one list — not one error per run until they are all gone."""
    with pytest.raises(ConfigError) as caught:
        parse("port = 8080\nhost = 'localhost'\n" + MINIMAL.replace(
            'type = "ingest_concorde"', 'type = "ingest_gibberish"'))

    message = str(caught.value)
    assert "port" in message
    assert "host" in message
    assert "ingest_gibberish" in message
    assert "3 problem(s)" in message


def test_a_missing_config_file_names_the_example(tmp_path):
    with pytest.raises(ConfigError, match="config.toml.example"):
        load_config(tmp_path / "nowhere.toml")


def test_looking_up_an_unconfigured_chain_is_an_error():
    config = parse(MINIMAL)

    with pytest.raises(ConfigError, match="no \\[ingest_chain.typo\\] block"):
        config.chain("ingest_chain", "typo")


def test_the_snapshot_chain_is_looked_up_by_its_family_name():
    config = parse(MINIMAL)

    assert config.chain("snapshot_chain", "snapshot_chain") is config.snapshot_chain
