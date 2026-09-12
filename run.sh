#! /bin/bash
#
# Starts the Concorde service and all five chains as one profile.
# Ctrl-C stops the lot; if any one process dies, the rest are stopped too.
#
source .venv/bin/activate
python3 main.py run concorde_test
