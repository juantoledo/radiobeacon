from adapters.__main__ import discover_adapters
from adapters.csn import CsnAdapter
from adapters.senapred import SenapredAdapter


def test_discover_adapters_finds_senapred():
    classes = discover_adapters()

    assert SenapredAdapter in classes


def test_discover_adapters_finds_csn():
    classes = discover_adapters()

    assert CsnAdapter in classes


def test_discover_adapters_excludes_base_contract():
    from adapters.base import DataSourceAdapter

    classes = discover_adapters()

    assert DataSourceAdapter not in classes
