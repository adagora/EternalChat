"""Tests never read or write the user's saved settings: they get a home of their own,
which subprocesses inherit."""
import atexit
import os
import shutil
import tempfile

os.environ["ETERNALCHAT_HOME"] = tempfile.mkdtemp(prefix="eternalchat-test-home-")
atexit.register(shutil.rmtree, os.environ["ETERNALCHAT_HOME"], ignore_errors=True)
