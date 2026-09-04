"""Invariants for the /config landing-page section layout
(ui.config_catalog.CATEGORY_LAYOUT / sections_for_category) — pure data
checks, no app/DB needed. Catches a SETTINGS_CATALOG group that was added,
renamed, or removed without updating the matching Section layout."""
from ui.config_catalog import CATEGORY_LAYOUT, groups_for_category, sections_for_category


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


def test_unlisted_category_falls_back_to_one_unlabeled_section():
    sections = sections_for_category("MQ")

    assert len(sections) == 1
    assert sections[0].label == ""
    assert set(sections[0].groups) == set(groups_for_category("MQ"))
