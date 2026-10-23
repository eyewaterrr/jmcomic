from jmshelf.ids import extract


def albums(text):
    return [n for kind, n in extract(text) if kind == "album"]


def test_plain_and_prefixed():
    assert albums("1485198") == [1485198]
    assert albums("JM1485198") == [1485198]
    assert albums("jm 1485198") == [1485198]
    assert albums("JM-1485198") == [1485198]
    assert albums("#JM1485198") == [1485198]
    assert albums("车号：350234") == [350234]
    assert albums("禁漫 350234 很好看") == [350234]


def test_full_width():
    assert albums("ＪＭ１４８５１９８") == [1485198]
    assert albums("１４８５１９８") == [1485198]


def test_multiple_ids():
    assert albums("123456 234567,345678\n456789") == [123456, 234567, 345678, 456789]
    assert albums("推荐 JM111111 和 jm222222，还有 JM111111") == [111111, 222222]


def test_urls():
    assert extract("https://18comic.vip/album/1485198/some-title") == [("album", 1485198)]
    assert extract("看这个 18comic.org/photo/350234") == [("photo", 350234)]
    assert extract("https://jmcomic-zzz.one/album/123456 和 JM234567") == [("album", 123456), ("album", 234567)]
    # 按输入顺序排列，链接不会被提到最前面
    assert extract("JM111111 18comic.vip/album/222222 JM333333") == [
        ("album", 111111), ("album", 222222), ("album", 333333)]


def test_keywords_are_not_ids():
    assert extract("原神") == []
    assert extract("C108 合集") == []
    assert extract("原神 123456") == []
    assert extract("12") == []
    assert extract("bjm123456") == []
