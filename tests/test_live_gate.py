import json
import time

from conftest import NOON
from kalshibot.cli import confirm_live, paper_gate
from kalshibot.storage import Storage


def _paper_db(cfg, tmp_path, n, pnl_each):
    cfg.db_path = str(tmp_path / "kb_{mode}.db")
    st = Storage(cfg.resolved_db_path("paper"))
    for i in range(n):
        side_won = pnl_each > 0
        pid = st.add_to_position(f"T{i}", "S", "yes", 1, 0.5, 0.0, NOON, NOON, 0.5, 0.0, None)
        st.settle_position(pid, "yes" if side_won else "no", NOON)
    st.close()


def test_live_refused_when_mode_not_live(cfg):
    assert confirm_live(cfg)[0] is False


def test_live_refused_without_paper_record(cfg, tmp_path):
    cfg.mode = "live"
    _paper_db(cfg, tmp_path, 10, +0.5)
    ok, msg = confirm_live(cfg, input_fn=lambda _: cfg.live.confirmation_phrase, isatty=True)
    assert not ok and "REFUSING LIVE" in msg


def test_live_refused_with_negative_paper_pnl(cfg, tmp_path):
    cfg.mode = "live"
    _paper_db(cfg, tmp_path, 300, -0.5)
    assert paper_gate(cfg)[0] is False


def test_live_needs_exact_typed_phrase(cfg, tmp_path):
    cfg.mode = "live"
    _paper_db(cfg, tmp_path, 300, +0.5)
    assert paper_gate(cfg)[0] is True
    assert confirm_live(cfg, input_fn=lambda _: "yes", isatty=True)[0] is False
    assert confirm_live(cfg, input_fn=lambda _: cfg.live.confirmation_phrase, isatty=True)[0] is True


def test_non_interactive_needs_matching_arm_file(cfg, tmp_path):
    cfg.mode = "live"
    _paper_db(cfg, tmp_path, 300, +0.5)
    cfg.live.armed_file = str(tmp_path / "ARMED")
    assert confirm_live(cfg, isatty=False)[0] is False
    with open(cfg.live.armed_file, "w") as fh:
        json.dump({"fingerprint": cfg.fingerprint(), "armed_at": time.time()}, fh)
    assert confirm_live(cfg, isatty=False)[0] is True
    cfg.risk.max_risk_per_trade = 5.0  # config changed after arming -> refuse
    assert confirm_live(cfg, isatty=False)[0] is False


def test_arm_file_expires(cfg, tmp_path):
    cfg.mode = "live"
    _paper_db(cfg, tmp_path, 300, +0.5)
    cfg.live.armed_file = str(tmp_path / "ARMED")
    with open(cfg.live.armed_file, "w") as fh:
        json.dump({"fingerprint": cfg.fingerprint(), "armed_at": time.time() - 25 * 3600}, fh)
    assert confirm_live(cfg, isatty=False)[0] is False
