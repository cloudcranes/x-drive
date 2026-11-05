from app import coverstore


def test_public_url_maps_javbus_and_old_proxy(monkeypatch):
    monkeypatch.setattr(coverstore, "R2_BASE", "https://covers.master.us.kg")
    want = "https://covers.master.us.kg/javbus/www.javbus.com/pics/cover/68hp_b.jpg"
    assert coverstore.public_url("https://www.javbus.com/pics/cover/68hp_b.jpg") == want
    assert coverstore.public_url("https://javbus.com/pics/cover/68hp_b.jpg") == want
    assert coverstore.public_url("https://javbus.img.master.us.kg/pics/cover/68hp_b.jpg") == want


def test_public_url_is_idempotent_and_keeps_unknown_hosts(monkeypatch):
    monkeypatch.setattr(coverstore, "R2_BASE", "https://covers.master.us.kg")
    r2 = "https://covers.master.us.kg/javbus/www.javbus.com/pics/cover/68hp_b.jpg"
    assert coverstore.public_url(r2) == r2
    assert coverstore.public_url("https://example.com/a.jpg") == "https://example.com/a.jpg"
    assert coverstore.public_url("") == ""


def test_public_url_maps_dmm_preview(monkeypatch):
    monkeypatch.setattr(coverstore, "R2_BASE", "https://covers.master.us.kg")
    want = "https://covers.master.us.kg/javbus/pics.dmm.co.jp/digital/video/tchr00022/tchr00022jp-1.jpg"
    assert (coverstore.public_url("https://pics.dmm.co.jp/digital/video/tchr00022/tchr00022jp-1.jpg")
            == want)
    assert (coverstore.public_url("https://awsimgsrc.dmm.co.jp/x.jpg")
            == "https://covers.master.us.kg/javbus/awsimgsrc.dmm.co.jp/x.jpg")


def test_source_url_uses_worker_path(monkeypatch):
    monkeypatch.setattr(coverstore, "SOURCE_BASE", "https://javbus.img.master.us.kg")
    assert (coverstore.source_url("https://www.javbus.com/pics/cover/68hp_b.jpg")
            == "https://javbus.img.master.us.kg/pics/cover/68hp_b.jpg")


def test_source_url_uses_worker_url_param_for_dmm(monkeypatch):
    monkeypatch.setattr(coverstore, "SOURCE_BASE", "https://javbus.img.master.us.kg")
    want = ("https://javbus.img.master.us.kg/?url=https%3A%2F%2Fpics.dmm.co.jp"
            "%2Fdigital%2Fvideo%2Ftchr00022%2Ftchr00022jp-1.jpg")
    assert (coverstore.source_url("https://pics.dmm.co.jp/digital/video/tchr00022/tchr00022jp-1.jpg")
            == want)


def test_warm_fetches_r2_miss_through_worker(monkeypatch):
    monkeypatch.setattr(coverstore, "R2_BASE", "https://covers.master.us.kg")
    monkeypatch.setattr(coverstore, "SOURCE_BASE", "https://javbus.img.master.us.kg")
    calls = []
    monkeypatch.setattr(coverstore, "_ok", lambda url: False)
    monkeypatch.setattr(coverstore, "_fetch", lambda url: calls.append(url) or True)
    dest = coverstore.warm("https://www.javbus.com/pics/cover/68hp_b.jpg")
    assert dest == "https://covers.master.us.kg/javbus/www.javbus.com/pics/cover/68hp_b.jpg"
    assert calls == ["https://javbus.img.master.us.kg/pics/cover/68hp_b.jpg"]


def test_warm_skips_fetch_when_r2_hit(monkeypatch):
    monkeypatch.setattr(coverstore, "R2_BASE", "https://covers.master.us.kg")
    monkeypatch.setattr(coverstore, "_ok", lambda url: True)
    monkeypatch.setattr(coverstore, "_fetch", lambda url: (_ for _ in ()).throw(AssertionError(url)))
    coverstore.warm("https://www.javbus.com/pics/cover/68hp_b.jpg")


def test_source_url_accepts_r2_url(monkeypatch):
    monkeypatch.setattr(coverstore, "SOURCE_BASE", "https://javbus.img.master.us.kg")
    r2 = "https://covers.master.us.kg/javbus/www.javbus.com/pics/cover/amig_b.jpg"
    assert coverstore.source_url(r2) == "https://javbus.img.master.us.kg/pics/cover/amig_b.jpg"


def test_backfill_warms_missing_r2_object(monkeypatch):
    monkeypatch.setattr(coverstore, "R2_BASE", "https://covers.master.us.kg")
    monkeypatch.setattr(coverstore, "SOURCE_BASE", "https://javbus.img.master.us.kg")
    monkeypatch.setattr(coverstore, "_ok", lambda url: False)
    calls = []
    monkeypatch.setattr(coverstore, "_fetch", lambda url: calls.append(url) or True)
    out = coverstore.backfill(["https://covers.master.us.kg/javbus/www.javbus.com/pics/cover/amig_b.jpg"])
    assert out["warmed"] == ["https://covers.master.us.kg/javbus/www.javbus.com/pics/cover/amig_b.jpg"]
    assert calls == ["https://javbus.img.master.us.kg/pics/cover/amig_b.jpg"]


def test_source_url_accepts_r2_dmm_url(monkeypatch):
    monkeypatch.setattr(coverstore, "SOURCE_BASE", "https://javbus.img.master.us.kg")
    r2 = "https://covers.master.us.kg/javbus/pics.dmm.co.jp/digital/video/tchr00022/tchr00022jp-1.jpg"
    assert (coverstore.source_url(r2)
            == "https://javbus.img.master.us.kg/?url=https%3A%2F%2Fpics.dmm.co.jp"
               "%2Fdigital%2Fvideo%2Ftchr00022%2Ftchr00022jp-1.jpg")


def test_warm_fetches_dmm_through_worker_url(monkeypatch):
    monkeypatch.setattr(coverstore, "R2_BASE", "https://covers.master.us.kg")
    monkeypatch.setattr(coverstore, "SOURCE_BASE", "https://javbus.img.master.us.kg")
    calls = []
    monkeypatch.setattr(coverstore, "_ok", lambda url: False)
    monkeypatch.setattr(coverstore, "_fetch", lambda url: calls.append(url) or True)
    dest = coverstore.warm("https://pics.dmm.co.jp/digital/video/tchr00022/tchr00022jp-1.jpg")
    assert dest == "https://covers.master.us.kg/javbus/pics.dmm.co.jp/digital/video/tchr00022/tchr00022jp-1.jpg"
    assert calls == ["https://javbus.img.master.us.kg/?url=https%3A%2F%2Fpics.dmm.co.jp"
                     "%2Fdigital%2Fvideo%2Ftchr00022%2Ftchr00022jp-1.jpg"]


def test_warm_meta_skips_before_warmer_started(monkeypatch):
    monkeypatch.setattr(coverstore, "_started", False)
    monkeypatch.setattr(coverstore, "warm", lambda url: (_ for _ in ()).throw(AssertionError(url)))
    coverstore.warm_meta({"cover": "https://www.javbus.com/pics/cover/68hp_b.jpg"})


def test_warm_meta_warms_cover_and_previews(monkeypatch):
    monkeypatch.setattr(coverstore, "_started", True)
    monkeypatch.setattr(coverstore, "R2_BASE", "https://covers.master.us.kg")
    monkeypatch.setattr(coverstore, "SOURCE_BASE", "https://javbus.img.master.us.kg")
    monkeypatch.setattr(coverstore, "_ok", lambda url: False)
    calls = []
    monkeypatch.setattr(coverstore, "_fetch", lambda url: calls.append(url) or True)
    coverstore.warm_meta({
        "cover": "https://www.javbus.com/pics/cover/68hp_b.jpg",
        "previews": ["https://pics.dmm.co.jp/digital/video/tchr00022/tchr00022jp-1.jpg"],
    }, background=False)
    assert calls == [
        "https://javbus.img.master.us.kg/pics/cover/68hp_b.jpg",
        "https://javbus.img.master.us.kg/?url=https%3A%2F%2Fpics.dmm.co.jp"
        "%2Fdigital%2Fvideo%2Ftchr00022%2Ftchr00022jp-1.jpg",
    ]
