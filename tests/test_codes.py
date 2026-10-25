
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


def test_canonical_reads_letter_suffix_after_number():
    """番号后面粘连着字母后缀也要认出来：`MIMK-267ch`（中文版）。

    实测踩过：云添加落地目录名是 `mimk-186ch`，番号认不出来 →
    归档目录用了磁力原名，站内既不是番号、也跟别的记录对不上。
    """
    from app import codes
    cases = {
        "MIMK-267ch": "MIMK-267",
        "mimk-186ch": "MIMK-186",
        "mimk-186ch.mp4": "MIMK-186",
        "MIMK-289ch": "MIMK-289",
        "ABF-372CH": "ABF-372",
    }
    for raw, want in cases.items():
        assert codes.canonical(raw) == want, raw


def test_canonical_still_rejects_codec_suffixes():
    """编码/画质标记不是「中文版」后缀：不能把它们当番号认出来。

    `MIMK-267H264` 里 H264 是编码标记 —— 整个串不是番号，认成 MIMK-267 会
    把一条无关的资源归到这部片名下。
    """
    from app import codes
    assert codes.canonical("MIMK-267H264") == ""
    # 连字符写法不受影响：U 跟在连字符后面仍然是多语种版标记
    assert codes.canonical("MIMK-285-U") == "MIMK-285"
