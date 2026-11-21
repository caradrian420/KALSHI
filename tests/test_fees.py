import pytest

from kalshibot.fees import FeeModel, ceil_to_cent, fee_per_contract, maker_fee, quadratic_fee, taker_fee


def test_ceil_to_cent_exact_and_fractional():
    assert float(ceil_to_cent(0.0175)) == 0.02
    assert float(ceil_to_cent(1.75)) == 1.75          # exact cents are NOT bumped
    assert float(ceil_to_cent(1.7500001)) == 1.76
    assert float(ceil_to_cent(0)) == 0.0


def test_taker_fee_matches_published_examples():
    # 100 contracts at $0.50: 0.07*100*0.25 = 1.75 (Kalshi's commonly cited example)
    assert taker_fee(100, 0.50) == 1.75
    # float-fuzz trap: 0.07*100*0.5*0.5 == 1.7500000000000002 in binary float
    assert taker_fee(100, 0.50) != 1.76


def test_maker_fee_example():
    # 0.0175*100*0.25 = 0.4375 -> 0.44
    assert maker_fee(100, 0.50) == 0.44


@pytest.mark.parametrize("count,price,expected", [
    (1, 0.50, 0.02),   # 0.0175 -> 0.02 : cent minimum dominates tiny orders
    (1, 0.01, 0.01),   # 0.000693 -> 0.01
    (1, 0.99, 0.01),
    (2, 0.50, 0.04),   # 0.035 -> 0.04
    (3, 0.40, 0.06),   # 0.0504 -> 0.06
    (10, 0.90, 0.07),  # 0.063 -> 0.07
    (5, 0.20, 0.06),   # 0.056 -> 0.06
])
def test_taker_fee_rounding_small_orders(count, price, expected):
    assert taker_fee(count, price) == expected


def test_fee_zero_cases():
    assert taker_fee(0, 0.5) == 0.0
    assert quadratic_fee(5, 0.0, 0.07) == 0.0
    assert quadratic_fee(5, 1.0, 0.07) == 0.0


def test_fee_per_contract_penalises_small_orders():
    # 1 contract pays 2c/contract; 100 contracts pay 1.75c/contract
    assert fee_per_contract(1, 0.5, 0.07) == pytest.approx(0.02)
    assert fee_per_contract(100, 0.5, 0.07) == pytest.approx(0.0175)


def test_fee_symmetric_in_price():
    for n in (1, 3, 7, 50):
        for p in (0.05, 0.2, 0.37, 0.5):
            assert taker_fee(n, p) == taker_fee(n, round(1 - p, 2))


def test_fee_model_series_types():
    q = FeeModel(fee_type="quadratic", fee_multiplier=1)
    assert q.maker(100, 0.5) == 0.0 and q.taker(100, 0.5) == 1.75
    qm = FeeModel(fee_type="quadratic_with_maker_fees", fee_multiplier=1)
    assert qm.maker(100, 0.5) == 0.44
    combo = FeeModel(fee_type="quadratic_with_combo_maker_fees", fee_multiplier=1)
    assert combo.maker_rate == pytest.approx(0.035)
    assert FeeModel(fee_type="flat").supported is False
    assert FeeModel(fee_type="something_new").supported is False
    # unknown fee_type (None) -> assume maker fees apply (conservative)
    assert FeeModel(fee_type=None).maker_rate == pytest.approx(0.0175)


def test_fee_model_multiplier_never_lowers_fees():
    assert FeeModel(fee_multiplier=0.5).taker_rate == pytest.approx(0.07)
    assert FeeModel(fee_multiplier=2.0).taker_rate == pytest.approx(0.14)
    assert FeeModel(fee_multiplier=2.0, apply_series_multiplier=False).taker_rate == pytest.approx(0.07)
