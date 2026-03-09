from services.diff_render_helpers import _build_inline_change_html


def test_inline_highlight_uses_word_level_for_identifier_changes():
    old_html, new_html, changed = _build_inline_change_html(
        "N_COVER_TOGGLE_TYPE = T_COVER_TOGGLE_TYPE.NONE",
        "N_COVER_TOGGLE_TYPE = T_COVER_TOGGLE_TYPE.CROUCH",
    )

    assert changed is True
    assert '<span class="diff-inline-removed">NONE</span>' in old_html
    assert '<span class="diff-inline-added">CROUCH</span>' in new_html
    assert '<span class="diff-inline-removed">N</span>' not in old_html
    assert '<span class="diff-inline-added">C</span>' not in new_html


def test_inline_highlight_skips_lua_control_keywords():
    old_html, new_html, changed = _build_inline_change_html(
        "if role_state then end",
        "while role_state do end",
    )

    assert changed is False
    assert "diff-inline-added" not in new_html
    assert "diff-inline-removed" not in old_html


def test_weekly_diff_wrapper_uses_high_height_fallback():
    with open("services/diff_render_helpers.py", "r", encoding="utf-8") as file:
        content = file.read()

    assert "max-height: 20000px;" in content
    assert "overflow-y: visible;" in content
    assert "max-height: 70vh;" not in content
