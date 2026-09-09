from actions.__main__ import discover_actions
from actions.chunk import ChunkAction


def test_discover_actions_finds_chunk():
    classes = discover_actions()

    assert ChunkAction in classes


def test_discover_actions_excludes_base_contract():
    from actions.base import Action

    classes = discover_actions()

    assert Action not in classes
