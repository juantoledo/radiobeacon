from adapters.storage import get_setting, set_setting

from ui.setup import SETUP_WIZARD_STEPS, is_setup_complete

IDENTITY_VALUES = {
    "BEACON_CALLSIGN": "N0CALL-1",
    "BEACON_DESCRIPTION": "Experimental propagation beacon",
    "BEACON_SHORT_DESCRIPTION": "Experimental propagation beacon",
    "BEACON_OPERATOR_CONTACT": "operator@example.com",
    "BEACON_GRID_LOCATOR": "FF46vb",
    "BEACON_FREQUENCY": "TX frequency",
}

# One valid submission per step, keyed by step slug — used to walk the whole
# wizard end to end.
STEP_VALUES = {
    "identity": IDENTITY_VALUES,
    "transmission": {"BEACON_ENABLED": "false", "BEACON_TYPE": "voice"},
    "wav-handoff": {
        "BEACON_WAV_TRANSMITTER": "logging",
        "BEACON_TXQUEUE_INCOMING_DIR": "/var/spool/svxlink-tx/incoming",
    },
    "voice": {
        "BEACON_TTS_ENGINE": "piper",
        "BEACON_TTS_VOICE": "es",
        "BEACON_TTS_PIPER_MODEL": "storage/piper_voices/es_MX-claude-high.onnx",
    },
    "watermark": {"BEACON_WATERMARK_ENABLED": "false"},
    "ai": {"ACTIONS_AI_ENABLED": "false"},
    "display": {
        "DISPLAY_TIMEZONE": "America/Santiago",
        "UI_DEFAULT_LOCALE": "en",
        "UI_DEFAULT_THEME": "system",
    },
}


# --- enforcement ---


def test_fresh_install_redirects_gated_route_to_setup(fresh_install_client):
    response = fresh_install_client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_gated_route_no_longer_redirects_once_setup_complete(fresh_install_client):
    fresh_install_client.post("/setup/finish", follow_redirects=False)

    response = fresh_install_client.get("/", follow_redirects=False)

    assert response.status_code == 200


def test_non_admin_never_redirected_to_setup(fresh_install_user_client):
    response = fresh_install_user_client.get("/items", follow_redirects=False)

    assert response.status_code == 200


# --- wizard navigation ---


def test_setup_root_redirects_to_first_step(fresh_install_client):
    response = fresh_install_client.get("/setup", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == f"/setup/{SETUP_WIZARD_STEPS[0].slug}"


def test_setup_wizard_first_step_is_beacon_identity(fresh_install_client):
    response = fresh_install_client.get(f"/setup/{SETUP_WIZARD_STEPS[0].slug}")

    assert response.status_code == 200
    assert "BEACON_CALLSIGN" in response.text


def test_unknown_setup_step_404s(fresh_install_client):
    response = fresh_install_client.get("/setup/not-a-real-step")

    assert response.status_code == 404


def test_setup_wizard_save_advances_to_next_step_and_persists(fresh_install_client, conn):
    response = fresh_install_client.post(
        "/setup/identity", data=IDENTITY_VALUES, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/setup/transmission"
    assert get_setting("BEACON_CALLSIGN", conn=conn, env_fallback=False) == "N0CALL-1"


def test_setup_wizard_required_field_validation_still_applies(fresh_install_client):
    incomplete = dict(IDENTITY_VALUES, BEACON_CALLSIGN="")

    response = fresh_install_client.post(
        "/setup/identity", data=incomplete, follow_redirects=False
    )

    assert response.status_code == 400
    assert "Required" in response.text
    # Already-entered fields are preserved in the re-rendered form.
    assert "operator@example.com" in response.text


def test_setup_wizard_walks_every_step_in_order(fresh_install_client):
    last_response = None
    for step in SETUP_WIZARD_STEPS:
        last_response = fresh_install_client.post(
            f"/setup/{step.slug}", data=STEP_VALUES[step.slug], follow_redirects=False
        )
        assert last_response.status_code == 303

    assert last_response.headers["location"] == "/setup/finish"


# --- UX: timezone select, conditional fields, optional-step skip link ---


def test_display_step_renders_timezone_as_a_select(fresh_install_client):
    response = fresh_install_client.get("/setup/display")

    assert response.status_code == 200
    assert '<select id="DISPLAY_TIMEZONE"' in response.text
    assert '<option value="America/Santiago" selected>' in response.text
    assert '<option value="Europe/London"' in response.text


def test_ai_step_marks_provider_specific_fields_conditional(fresh_install_client):
    response = fresh_install_client.get("/setup/ai")

    assert response.status_code == 200
    assert 'data-show-if="ACTIONS_AI_ENABLED=true"' in response.text
    assert (
        'data-show-if="ACTIONS_AI_ENABLED=true&amp;ACTIONS_AI_PROVIDER=claude"'
        in response.text
    )
    assert (
        'data-show-if="ACTIONS_AI_ENABLED=true&amp;ACTIONS_AI_PROVIDER=openai"'
        in response.text
    )


def test_watermark_step_marks_interval_conditional(fresh_install_client):
    response = fresh_install_client.get("/setup/watermark")

    assert response.status_code == 200
    assert 'data-show-if="BEACON_WATERMARK_ENABLED=true"' in response.text


def test_optional_step_shows_skip_link(fresh_install_client):
    response = fresh_install_client.get("/setup/watermark")

    assert response.status_code == 200
    assert 'href="/setup/ai"' in response.text  # the skip link's target


def test_required_step_has_no_skip_link(fresh_install_client):
    response = fresh_install_client.get("/setup/identity")

    assert response.status_code == 200
    assert "Skip for now" not in response.text
    assert 'href="/setup/transmission"' not in response.text


# --- finish ---


def test_finish_step_marks_setup_complete_and_redirects_home(fresh_install_client, conn):
    response = fresh_install_client.post("/setup/finish", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert is_setup_complete(conn) is True


def test_finish_page_shows_up_for_a_fresh_install(fresh_install_client):
    response = fresh_install_client.get("/setup/finish")

    assert response.status_code == 200


# --- voluntary reopen ---


def test_admin_can_reopen_completed_wizard(client):
    # `client` seeds SETUP_WIZARD_COMPLETED=true by default (see conftest.py).
    response = client.get("/setup/identity")

    assert response.status_code == 200


def test_reopened_wizard_prefills_current_values(client, conn):
    set_setting(conn, "BEACON_CALLSIGN", "N0CALL-1")

    response = client.get("/setup/identity")

    assert "N0CALL-1" in response.text


def test_reopening_and_finishing_again_is_a_harmless_no_op(client, conn):
    response = client.post("/setup/finish", follow_redirects=False)

    assert response.status_code == 303
    assert is_setup_complete(conn) is True
