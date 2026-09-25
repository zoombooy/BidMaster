"""归一化单元测试：中文大写金额 / 阿拉伯金额 / 中文日期 / 工期。"""
from bidmaster.extraction.normalize import (cn_to_number, normalize_amount,
                                            normalize_datetime, normalize_duration)


class TestCnNumber:
    def test_simple(self):
        assert cn_to_number("伍佰") == 500
        assert cn_to_number("三千五百") == 3500
        assert cn_to_number("一百二十") == 120
        assert cn_to_number("十") == 10
        assert cn_to_number("拾伍") == 15

    def test_big(self):
        assert cn_to_number("二十万") == 200000
        assert cn_to_number("一千二百万") == 12000000
        assert cn_to_number("贰亿伍仟万") == 250000000
        assert cn_to_number("捌拾万") == 800000


class TestAmount:
    def test_uppercase_with_paren(self):
        r = normalize_amount("人民币贰亿伍仟万元整（¥250,000,000.00）")
        assert r and r[0] == 250_000_000

    def test_plain_cn(self):
        r = normalize_amount("人民币捌拾万元整")
        assert r and r[0] == 800_000

    def test_arabic_wan(self):
        r = normalize_amount("3,500.5万元")
        assert r and r[0] == 35_005_000

    def test_yi(self):
        r = normalize_amount("1.2亿元")
        assert r and r[0] == 120_000_000


class TestDatetime:
    def test_full(self):
        r = normalize_datetime("2026年11月20日09时30分")
        assert r and r[0] == "2026-11-20 09:30"

    def test_cn(self):
        r = normalize_datetime("二〇二六年十月一日")
        assert r and r[0] == "2026-10-01"

    def test_iso(self):
        r = normalize_datetime("2026-11-20")
        assert r and r[0] == "2026-11-20"

    def test_with_suffix(self):
        r = normalize_datetime("2026年11月20日09时30分（下同）")
        assert r and r[0] == "2026-11-20 09:30"


class TestDuration:
    def test_days(self):
        r = normalize_duration("540 日历天")
        assert r and r[0] == 540 and r[1] == "日历天"

    def test_months(self):
        r = normalize_duration("三个月")
        assert r and r[0] == 90

    def test_invalid(self):
        assert normalize_duration("详见第五章") is None
