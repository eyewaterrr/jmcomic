import hashlib
import hmac
import json
import time
import urllib.parse

from jmshelf.fmt import text_table
from jmshelf.web import PageCache, check_init_data, make_token, read_token

TOKEN = "123456:TEST-token"


def signed_init_data(user_id=42, auth_date=None, extra=None):
    fields = {
        "auth_date": str(int(auth_date or time.time())),
        "query_id": "AAE",
        "user": json.dumps({"id": user_id, "first_name": "测试"}, ensure_ascii=False),
        "start_param": "c123",
    }
    fields.update(extra or {})
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


def test_init_data_valid():
    info = check_init_data(signed_init_data(), TOKEN)
    assert info["user"]["id"] == 42 and info["start_param"] == "c123"


def test_init_data_with_signature_field():
    assert check_init_data(signed_init_data(extra={"signature": "abc"}), TOKEN)["user"]["id"] == 42


def test_init_data_rejects_tampering_wrong_token_and_expiry():
    data = signed_init_data()
    assert check_init_data(data.replace("c123", "c999"), TOKEN) is None
    assert check_init_data(data, "999:other") is None
    assert check_init_data(signed_init_data(auth_date=time.time() - 3 * 86400), TOKEN) is None
    assert check_init_data("", TOKEN) is None


def test_token_roundtrip_and_expiry():
    key = b"k" * 32
    token = make_token(key, 7, ttl=60)
    assert read_token(key, token) == 7
    assert read_token(b"x" * 32, token) is None
    assert read_token(key, token, now=time.time() + 120) is None
    assert read_token(key, "garbage") is None


def test_page_cache_evicts_oldest(tmp_path):
    cache = PageCache(tmp_path, max_bytes=250)
    for i in range(1, 4):
        cache.put(1, i, ".webp", b"x" * 100)
        time.sleep(0.01)
    assert cache.total <= 225
    assert cache.get(1, 1, ".webp") is None
    assert cache.get(1, 3, ".webp") == b"x" * 100


def test_text_table_alignment():
    out = text_table(["ID", "NAME"], [[1, "ab"], [123, "c"]], right={0})
    assert out == "<code> ID  NAME\n---  ----\n  1  ab\n123  c</code>"
