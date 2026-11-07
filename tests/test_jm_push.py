from jmshelf.jm import JM


class FakePage:
    def __init__(self, ids, total):
        self._ids, self.total = ids, total

    def iter_folder_id_name(self):
        return iter([])

    def iter_id(self):
        return iter(str(i) for i in self._ids)


class FakeClient:
    """模拟 JM 移动端：收藏接口是“切换”，分页每页 2 条。"""

    def __init__(self, favorites, broken=()):
        self.favorites = list(favorites)
        self.broken = set(broken)
        self.calls = []

    def favorite_folder(self, page, folder_id):
        start = (page - 1) * 2
        return FakePage(self.favorites[start:start + 2], len(self.favorites))

    def add_favorite_album(self, album_id):
        self.calls.append(int(album_id))
        if int(album_id) in self.broken:
            raise RuntimeError("网络错误")
        if int(album_id) in self.favorites:
            raise RuntimeError("收藏操作不符合预期，期望 [add]，实际为 [remove]（已自动回滚还原收藏状态）")
        self.favorites.append(int(album_id))


def test_push_skips_existing_and_only_adds_missing():
    client = FakeClient([1, 2, 3, 4, 5])
    result = JM.push_with_client(client, [3, 6, 7, 6])
    assert result == {"added": [6, 7], "skipped": [3], "failed": []}
    assert client.calls == [6, 7]  # 已收藏的 3 没有调用切换接口


def test_push_reports_failures_and_rollback_as_skipped():
    client = FakeClient([1], broken={9})
    client.favorite_folder = lambda page, folder_id: FakePage([], 0)  # 读到的列表过期，以为 1 不在
    result = JM.push_with_client(client, [1, 9])
    assert result["skipped"] == [1]
    assert [i for i, _ in result["failed"]] == [9]
