"""Loop planning across take lengths × targets × fit policies (CLAUDE.md §5, Module D)."""
import math

import pytest

from core.renderer import TakeTooShortError, plan_render

TAKES_MIN = [3, 7, 30, 52, 600]
TARGETS_H = [1, 3, 4, 6, 8]
POLICIES = ["nearest", "at_least", "exact"]
INTRO_S = 65.0
BLACK_S = 60.0


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("target_h", TARGETS_H)
@pytest.mark.parametrize("take_min", TAKES_MIN)
def test_plan(take_min, target_h, policy):
    take = take_min * 60.0 + 7.3          # odd fractional lengths, not round numbers
    p = plan_render(take, INTRO_S, target_h, policy, black_loop_s=BLACK_S)
    target = target_h * 3600.0
    remaining = target - INTRO_S

    assert p.loops >= 1
    assert p.total_s == pytest.approx(INTRO_S + p.loops * take + p.tail_s)

    # black video always covers the audio, with less than one spare loop
    assert p.black_copies * BLACK_S >= p.total_s - 1e-6
    assert (p.black_copies - 1) * BLACK_S < p.total_s

    if remaining <= take:                 # take alone fills the target: used once, unlooped
        assert p.loops == 1 and p.tail_s == 0
        return

    if policy == "nearest":
        assert abs(p.total_s - target) <= take / 2 + 1e-6
        assert p.tail_s == 0
    elif policy == "at_least":
        assert p.total_s >= target - 1e-6
        assert p.total_s - target < take
        assert p.tail_s == 0
    else:
        assert abs(p.total_s - target) < 1.0
        assert 0 <= p.tail_s < take


def test_examples_from_design_doc():
    # 7 min take loops ~68× for 8 h; 52 min loops ~9×
    assert plan_render(7 * 60, INTRO_S, 8).loops == 68
    assert plan_render(52 * 60, INTRO_S, 8).loops == 9
    # 30 min take, 1:05 intro → ×16 by nearest
    assert plan_render(30 * 60, INTRO_S, 8).loops == 16


def test_take_longer_than_target_used_once():
    p = plan_render(10 * 3600, INTRO_S, 8, "exact")
    assert p.loops == 1 and p.tail_s == 0
    assert p.total_s == pytest.approx(INTRO_S + 10 * 3600)


def test_too_short_take_refused():
    with pytest.raises(TakeTooShortError):
        plan_render(12.0, INTRO_S, 8, seam_fade_s=1.5)   # needs ≥ 2×1.5 + 10 = 13 s


def test_minimum_take_allowed_but_warned():
    p = plan_render(13.0, INTRO_S, 8, seam_fade_s=1.5)
    assert p.loops > 1000
    assert p.warnings and "Short take" in p.warnings[0]


def test_no_warning_for_normal_take():
    assert plan_render(30 * 60, INTRO_S, 8).warnings == ()


def test_nearest_rounds_half_up():
    # remaining / take == 2.5 exactly → 3 loops (banker's rounding would give 2)
    take = 1000.0
    p = plan_render(take, intro_s=0.0, target_hours=2500 / 3600, policy="nearest")
    assert p.loops == 3


def test_exact_fit_has_no_tail_and_no_extra_loop():
    take = 1800.0
    target_h = (INTRO_S + 16 * take) / 3600
    for policy in POLICIES:
        p = plan_render(take, INTRO_S, target_h, policy)
        assert p.loops == 16 and p.tail_s == 0, policy


def test_tiny_tail_dropped():
    take = 1800.0
    target_h = (INTRO_S + 16 * take + 0.4) / 3600
    p = plan_render(take, INTRO_S, target_h, "exact")
    assert p.loops == 16 and p.tail_s == 0


def test_summary_format():
    # 65 + 14 × 2052 = 28 793 s. (CLAUDE.md's example says "8h 00m 53s"; its arithmetic is off.)
    p = plan_render(34 * 60 + 12, INTRO_S, 8)
    assert p.summary() == "Take 34:12 · intro 1:05 · ×14 → 7h 59m 53s"
    p = plan_render(30 * 60, INTRO_S, 8, "exact")
    assert p.summary() == "Take 30:00 · intro 1:05 · ×15 + 28:55 tail → 8h 00m 00s"


def test_bad_policy():
    with pytest.raises(ValueError):
        plan_render(1800, INTRO_S, 8, "longest")
