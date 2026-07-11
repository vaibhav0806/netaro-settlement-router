import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "load_test.py"
SPEC = importlib.util.spec_from_file_location("external_load_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
load_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(load_test)


def test_http_timeout_allows_serialized_concurrent_proof():
    timeout = load_test.http_timeout()

    assert timeout.connect == 10
    assert timeout.write == 30
    assert timeout.read == 120
    assert timeout.pool == 120
