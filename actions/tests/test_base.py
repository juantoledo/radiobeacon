import pytest

from actions.base import Action


def test_action_is_abstract_without_run():
    with pytest.raises(TypeError):
        Action()


def test_wiring_defaults_are_none_when_not_declared():
    """An action that declares no default_* wiring inherits None for all
    three — so __main__ skips it (with a warning) when nothing configures
    its subscribe topic, rather than guessing one."""

    class Bare(Action):
        def run(self, event, *, conn):
            return []

    assert Bare.default_subscribe_topic is None
    assert Bare.default_output_topic is None
    assert Bare.default_output_event_type is None
