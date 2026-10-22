
def test_canonical_normalises_to_spec():
    from app import codes
    cases = {
        "ssis_123": "SSIS-123",
        "SSIS 123": "SSIS-123",
        "fc2ppv1234567": "FC2-PPV-1234567",
        "MIMK-286-中文字幕": "MIMK-286",
        "madoubt.com 868362.com MIMK-290": "MIMK-290",
        "CAWD-891-C": "CAWD-891",
        "svdvd-953.mp4": "SVDVD-953",
        "heyzo1234": "HEYZO-1234",
    }
    for raw, want in cases.items():
        assert codes.canonical(raw) == want, raw

def test_canonical_prefers_separated_match_over_glued():
    """`SIS001` 这种粘连写法容易抢先命中，规范名要选 `MIMK-285`。"""
    from app import codes
    assert codes.canonical("第一會所新片@SIS001@MIMK-285-U") == "MIMK-285"

def test_canonical_leaves_unknown_empty():
    from app import codes
    assert codes.canonical("unclassified-20261022-010203") == ""
    assert codes.canonical("1080p x264") == ""
    assert codes.canonical("") == ""
