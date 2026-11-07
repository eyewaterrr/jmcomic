from jmshelf.jm import order_param


def test_time_range_only_applies_to_most_viewed():
    assert order_param("mv", "w") == "mv_w"
    assert order_param("mv", "m") == "mv_m"
    assert order_param("mv", "t") == "mv_t"
    assert order_param("mv", "a") == "mv"
    assert order_param("mr", "w") == "mr"   # 其他排序带时间会被接口忽略，干脆不拼
    assert order_param("tf", "m") == "tf"
