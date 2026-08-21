import pytest

from actions.base import Action


def test_action_is_abstract_without_run():
    with pytest.raises(TypeError):
        Action()
