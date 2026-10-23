import zipfile

from PIL import Image

from jmshelf import pack


def make_images(folder, count):
    folder.mkdir()
    paths = []
    for i in range(count):
        p = folder / f"{i + 1:05d}.webp"
        Image.new("RGB", (60 + i, 90), (i * 20 % 255, 80, 160)).save(p, quality=80)
        paths.append(p)
    return paths


def test_build_cbz_offsets_point_at_page_bytes(tmp_path):
    images = make_images(tmp_path / "img", 5)
    album = {"jm_id": 1, "title": "测试 <&>", "authors": ["A"], "works": [], "tags": ["t"],
             "chapters": [{"photo_id": 1, "sort": 1, "title": "第1章"}]}
    out = tmp_path / "a.cbz"
    pages, sha = pack.build_cbz(images, out, pack.comicinfo_xml(album, album["chapters"][0], 5))

    raw = out.read_bytes()
    assert len(pages) == 5 and len(sha) == 64
    with zipfile.ZipFile(out) as zf:
        assert pack.COMICINFO in zf.namelist()
        assert "测试 &lt;&amp;&gt;" in zf.read(pack.COMICINFO).decode()
        for page, src in zip(pages, images):
            assert raw[page.offset: page.offset + page.size] == src.read_bytes() == zf.read(page.name)
    assert [p.width for p in pages] == [60, 61, 62, 63, 64]
    assert pages[0].name == "0001.webp"


def test_pdf_volumes_and_metadata(tmp_path):
    import json
    import subprocess
    import sys

    images = make_images(tmp_path / "img", 4)
    album = {"jm_id": 7, "title": "测试", "authors": [], "works": [], "tags": [],
             "chapters": [{"photo_id": 7, "sort": 1, "title": "x"}]}
    cbz = tmp_path / "a.cbz"
    pack.build_cbz(images, cbz, pack.comicinfo_xml(album, album["chapters"][0], 4))
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "jm_id": 7, "out_dir": str(tmp_path / "pdf"), "name": "[JM7] 测试", "title": "测试", "author": "A",
        "quality": 85, "volume_bytes": 1, "cbz": [str(cbz)],
    }))
    out = subprocess.run([sys.executable, "-m", "jmshelf.pdf", str(spec)], capture_output=True, text=True, check=True)
    volumes = json.loads(out.stdout.strip().splitlines()[-1])["volumes"]
    assert [v["pages"] for v in volumes] == [1, 1, 1, 1]
    first = open(volumes[0]["path"], "rb").read()
    assert first.startswith(b"%PDF") and b"jm=7 vol=1/4 pages=1" in first


def test_list_images_sorted_and_filtered(tmp_path):
    make_images(tmp_path / "img", 3)
    (tmp_path / "img" / "notes.txt").write_text("x")
    assert [p.name for p in pack.list_images(tmp_path / "img")] == ["00001.webp", "00002.webp", "00003.webp"]
