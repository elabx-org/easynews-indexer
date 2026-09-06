import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EASYNEWS_USER", "test")
os.environ.setdefault("EASYNEWS_PASS", "test")

import pytest  # noqa: E402

import server  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_search_cache():
    """The search cache is module-global; never let one test's entries leak into the next."""
    server._search_cache_clear()
    yield
    server._search_cache_clear()
