"""Keep the test run away from a real Hermes profile.

Several hooks write under ``<HERMES_HOME>/bot-chat-agent/`` (``status.json``,
``memory.db``, ...). Without this, running the suite on a machine whose
``HERMES_HOME`` points at a live profile overwrites that profile's status card
state. The throw-away home is set before any test module is imported.
"""
import atexit
import os
import shutil
import tempfile

_TMP_HOME = tempfile.mkdtemp(prefix="bca-test-home-")
os.environ["HERMES_HOME"] = _TMP_HOME
atexit.register(shutil.rmtree, _TMP_HOME, ignore_errors=True)
