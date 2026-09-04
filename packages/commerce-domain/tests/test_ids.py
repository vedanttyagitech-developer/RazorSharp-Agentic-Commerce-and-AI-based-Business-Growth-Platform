from commerce_domain import uuid7


class TestUuid7:
    def test_version_and_variant(self):
        u = uuid7()
        assert u.version == 7
        assert (u.bytes[8] & 0xC0) == 0x80  # RFC 4122 variant

    def test_time_ordered(self):
        early = uuid7(_now_ms=1_700_000_000_000)
        later = uuid7(_now_ms=1_700_000_001_000)
        assert early.bytes < later.bytes

    def test_unique(self):
        assert len({uuid7() for _ in range(2000)}) == 2000
