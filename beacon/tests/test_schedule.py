import pytest

from beacon.schedule import Slot, WindowConfig, current_slot


@pytest.fixture
def config():
    # 90s cycle: 60s voice, 5s guard, 25s frame -- leaves 0s idle, but
    # exercises guard explicitly (unlike the 0-guard default).
    return WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=25, guard_seconds=5)


def test_window_config_rejects_zero_total_seconds():
    with pytest.raises(ValueError):
        WindowConfig(total_seconds=0, voice_seconds=0, frame_seconds=0)


def test_window_config_rejects_negative_seconds():
    with pytest.raises(ValueError):
        WindowConfig(total_seconds=90, voice_seconds=-1, frame_seconds=30)


def test_window_config_rejects_slots_exceeding_total():
    with pytest.raises(ValueError):
        WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=40)


def test_window_config_allows_slots_exactly_filling_total():
    WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30)  # no raise


def test_start_of_cycle_is_voice(config):
    state = current_slot(config, now=0.0)
    assert state.slot is Slot.VOICE
    assert state.cycle_index == 0
    assert state.elapsed_in_cycle == 0.0
    assert state.remaining_in_slot == 60.0


def test_mid_voice_slot(config):
    state = current_slot(config, now=30.0)
    assert state.slot is Slot.VOICE
    assert state.remaining_in_slot == 30.0


def test_voice_to_guard_boundary_is_guard(config):
    state = current_slot(config, now=60.0)
    assert state.slot is Slot.GUARD
    assert state.remaining_in_slot == 5.0


def test_mid_guard_slot(config):
    state = current_slot(config, now=62.5)
    assert state.slot is Slot.GUARD
    assert state.remaining_in_slot == 2.5


def test_guard_to_frame_boundary_is_frame(config):
    state = current_slot(config, now=65.0)
    assert state.slot is Slot.FRAME
    assert state.remaining_in_slot == 25.0


def test_mid_frame_slot(config):
    state = current_slot(config, now=80.0)
    assert state.slot is Slot.FRAME
    assert state.remaining_in_slot == 10.0


def test_frame_end_to_total_is_idle(config):
    # voice(60) + guard(5) + frame(25) = 90 = total_seconds -- no idle
    # remainder in this fixture; use a config with slack to test idle.
    slack_config = WindowConfig(total_seconds=100, voice_seconds=60, frame_seconds=25, guard_seconds=5)
    state = current_slot(slack_config, now=90.0)
    assert state.slot is Slot.IDLE
    assert state.remaining_in_slot == 10.0


def test_cycle_wraps_to_next_voice(config):
    state = current_slot(config, now=90.0)
    assert state.slot is Slot.VOICE
    assert state.cycle_index == 1
    assert state.elapsed_in_cycle == 0.0


def test_cycle_index_increments_across_multiple_cycles(config):
    state = current_slot(config, now=90.0 * 5 + 10.0)
    assert state.cycle_index == 5
    assert state.slot is Slot.VOICE
    assert state.elapsed_in_cycle == 10.0


def test_guard_seconds_zero_collapses_voice_directly_into_frame():
    config = WindowConfig(total_seconds=90, voice_seconds=60, frame_seconds=30, guard_seconds=0)
    just_before = current_slot(config, now=59.999)
    at_boundary = current_slot(config, now=60.0)
    assert just_before.slot is Slot.VOICE
    assert at_boundary.slot is Slot.FRAME  # no GUARD slot ever occurs


def test_cycle_started_at_is_consistent_within_a_cycle(config):
    a = current_slot(config, now=10.0)
    b = current_slot(config, now=70.0)
    assert a.cycle_started_at == b.cycle_started_at == 0.0
