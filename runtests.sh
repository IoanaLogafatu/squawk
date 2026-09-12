#! /bin/bash
#
# Handy script to save having to remember the commands.
#
source .venv/bin/activate
python -m pytest tests/ -v
