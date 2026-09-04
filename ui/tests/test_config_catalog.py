"""Invariants for the /config landing-page section layout
(ui.config_catalog.CATEGORY_LAYOUT / sections_for_category) — pure data
checks, no app/DB needed. Catches a SETTINGS_CATALOG group that was added,
renamed, or removed without updating the matching Section layout."""
from adapters.settings_registry import SETTINGS_REGISTRY
from ui.config_catalog import CATEGORY_LAYOUT, SETTINGS_CATALOG, groups_for_category, sections_for_category


def test_every_group_is_placed_in_exactly_one_declared_section():
    for category, sections in CATEGORY_LAYOUT.items():
        declared = [g for section in sections for g in section.groups]
        assert len(declared) == len(set(declared)), (
            f"{category}: a group is listed in more than one Section"
        )
        assert set(declared) == set(groups_for_category(category)), (
            f"{category}: CATEGORY_LAYOUT doesn't match SETTINGS_CATALOG's "
            f"groups for this category — a group was added/renamed/removed "
            f"without updating the layout"
        )


def test_sections_for_category_never_produces_an_other_bucket():
    # test_every_group_is_placed_in_exactly_one_declared_section already
    # proves this for categories in CATEGORY_LAYOUT; this is the same
    # invariant from sections_for_category's own output, so a future bug in
    # its "Other" fallback logic itself would also be caught here.
    for category in CATEGORY_LAYOUT:
        labels = [s.label for s in sections_for_category(category)]
        assert "Other" not in labels


def test_settings_registry_matches_catalog():
    """adapters.settings_registry.SETTINGS_REGISTRY is an independent,
    hand-checked copy of this catalog's {key: (type, choices)} — needed by
    adapters.config_transfer (import/export), which structurally can't
    import this ui-only module (see settings_registry.py's own docstring
    for why). This is the guard that keeps the copy honest: every non-secret
    key here must appear in the registry with the exact same (type,
    choices), every secret key must be entirely absent from it (secrets are
    never importable/exportable), and the registry must not carry any extra
    key this catalog doesn't have."""
    catalog_non_secret = {
        spec.key: (spec.type, spec.choices) for spec in SETTINGS_CATALOG if not spec.is_secret
    }
    catalog_secret_keys = {spec.key for spec in SETTINGS_CATALOG if spec.is_secret}

    assert SETTINGS_REGISTRY == catalog_non_secret
    assert not (catalog_secret_keys & SETTINGS_REGISTRY.keys())


def test_unlisted_category_falls_back_to_one_unlabeled_section():
    sections = sections_for_category("MQ")

    assert len(sections) == 1
    assert sections[0].label == ""
    assert set(sections[0].groups) == set(groups_for_category("MQ"))
