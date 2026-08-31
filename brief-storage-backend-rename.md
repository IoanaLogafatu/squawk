# Brief: one name for the storage backend

**Scope:** `config.py`, `storage/__init__.py`, `processor/processor.py`,
`ingestor/personal_adsb/ingestor.py`, `ingestor/concorde/ingestor.py`,
`docs/storage-guide.md`, `tests/test_config.py`, `tests/test_processor.py`,
`tests/test_storage_shared.py`.

Pure rename. No behaviour change, no config change.

---

## Problem

One concept, three names. `config.toml` declares `backend = "disk_drive"` under
`[storage]`. `config.py` reads that key into a dataclass field called `method`,
so every consumer writes `config.storage.method`. `get_storage()` takes a parameter
called `method` and raises `ValueError("Unknown storage method: ...")`. Someone
reading `config.storage.method` and grepping `config.toml` for `method` finds
nothing, and someone hitting the error message and grepping the code for "backend"
finds only the loader.

`backend` is the name to keep. It is the one in `config.toml`, which is where a
person looks first, and it is the word the docs already use for the concept.

## Change

Rename `StorageConfig.method` to `backend`, and `get_storage`'s first parameter
from `method` to `backend`. Update the error string to `Unknown storage backend:`.
Four call sites read the field — `processor/processor.py:29`,
`ingestor/personal_adsb/ingestor.py:124`, `ingestor/concorde/ingestor.py:255`, and
`tests/test_config.py:54`. Three tests match on the error string
(`tests/test_processor.py`, `tests/test_storage_shared.py` twice); update the
`pytest.raises(match=...)` patterns. Check `docs/storage-guide.md` for any prose
using "method" for this, and grep for stragglers — `_INSTANCES` is keyed on
`(method, data_dir)` inside `get_storage`, so the local variable and the key
comment want renaming too.

The `[storage] backend` key in `config.toml` and `config.toml.example` does not
change; it was already correct. Nothing else in the codebase uses the word
"method" for a storage concept, so a global find-and-replace within these files
is safe, but read the diff rather than trusting that. Full suite green afterwards
— currently 260 passing — and no test should need a logic change, only string
updates.
