"""Synthetic offline configuration; never read repository sessions or .env."""
import os
import tempfile
from pathlib import Path

_runtime = tempfile.TemporaryDirectory()
os.environ['API_ID'] = '123'
os.environ['API_HASH'] = 'offline_api_hash'
os.environ['BOT_TOKEN'] = '123456:offline_test_token'
os.environ['ALLOWED_IDS'] = '123'
os.environ['DB_FILE'] = str(Path(_runtime.name) / 'queue.sqlite')
os.environ.pop('DATA_DIR', None)
