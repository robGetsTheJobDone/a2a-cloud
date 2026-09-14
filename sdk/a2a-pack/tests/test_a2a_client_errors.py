import pytest

from a2a_pack import A2AError
from a2a_pack.a2a_client import _raise_for_a2a


def test_generic_error_is_a2a_error():
    with pytest.raises(A2AError) as ei:
        _raise_for_a2a("http://a/invoke/run", 500, "boom")
    assert ei.value.status_code == 500
    assert ei.value.detail == "boom"


def test_402_is_plain_a2a_error():
    with pytest.raises(A2AError) as ei:
        _raise_for_a2a("http://a/invoke/run", 402, '{"detail": {"reason": "x"}}')
    assert type(ei.value) is A2AError
    assert ei.value.status_code == 402


def test_non_json_body_tolerated():
    with pytest.raises(A2AError) as ei:
        _raise_for_a2a("http://a/invoke/run", 403, "<html>forbidden</html>")
    assert ei.value.status_code == 403
