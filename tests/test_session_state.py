from app.agent_react.session_state import (
    ConversationSessionState,
    build_session_state_after_turn,
    dump_session_state,
    load_session_state,
    render_session_state,
    render_session_state_for_model,
)


def test_load_session_state_defaults_from_empty_metadata() -> None:
    state = load_session_state({})

    assert state == ConversationSessionState()


def test_session_state_round_trips_under_metadata_namespace() -> None:
    state = ConversationSessionState(
        session_mode="research",
        session_goal="compare runtime designs",
        working_summary="Keep the session state lightweight.",
        waiting_for="approval",
        pending_user_question="Which repository should I use?",
        pending_user_reason="Repository target is ambiguous.",
        pending_user_expected_answer_type="choice",
        pending_user_choices=("jarvis", "nltk"),
        pending_user_turn_id=6,
        last_turn_id=7,
        last_turn_status="completed",
        last_assistant_summary="Session state captured.",
        updated_by_turn_id=7,
    )

    metadata = dump_session_state(state)
    loaded = load_session_state({"unrelated": True, **metadata})

    assert metadata["session"]["session_mode"] == "research"
    assert metadata["session"]["pending_user_question"] == "Which repository should I use?"
    assert loaded == state


def test_load_session_state_ignores_invalid_values() -> None:
    loaded = load_session_state({
        "session": {
            "session_mode": "invalid",
            "waiting_for": "invalid",
            "last_turn_id": "42",
            "updated_by_turn_id": "not-an-int",
        }
    })

    assert loaded.session_mode == "chat"
    assert loaded.waiting_for is None
    assert loaded.last_turn_id == 42
    assert loaded.updated_by_turn_id is None


def test_render_session_state_is_status_friendly() -> None:
    rendered = render_session_state(ConversationSessionState(session_mode="coding"))

    assert "Session State" in rendered
    assert "Mode: coding" in rendered
    assert "Goal: -" in rendered


def test_render_session_state_for_model_omits_debug_only_fields() -> None:
    rendered = render_session_state_for_model(
        ConversationSessionState(
            session_mode="research",
            session_goal="compare runtime designs",
            working_summary="Keep the context lightweight.",
            waiting_for="user",
            pending_user_question="Which repo should I inspect?",
            pending_user_expected_answer_type="choice",
            pending_user_choices=("jarvis", "nltk"),
            last_turn_id=7,
            last_turn_status="completed",
            last_assistant_summary="debug summary",
        )
    )

    assert rendered is not None
    assert "Conversation session state:" in rendered
    assert "Mode: research" in rendered
    assert "Goal: compare runtime designs" in rendered
    assert "Working summary: Keep the context lightweight." in rendered
    assert "Pending user clarification:" in rendered
    assert "Question: Which repo should I inspect?" in rendered
    assert "Choices: jarvis, nltk" in rendered
    assert "last_turn_id" not in rendered
    assert "debug summary" not in rendered


def test_render_session_state_for_model_omits_empty_chat_state() -> None:
    assert render_session_state_for_model(ConversationSessionState()) is None


def test_build_session_state_after_turn_preserves_working_summary() -> None:
    state = build_session_state_after_turn(
        ConversationSessionState(
            session_mode="research",
            session_goal="compare runtime designs",
            working_summary="Do not overwrite this conservatively maintained summary.",
            waiting_for="tool",
        ),
        turn_id=12,
        status="completed",
        assistant_reply="Finished the architecture review.\n\nNext step is context hardening.",
    )

    assert state.session_mode == "research"
    assert state.session_goal == "compare runtime designs"
    assert state.working_summary == "Do not overwrite this conservatively maintained summary."
    assert state.waiting_for is None
    assert state.pending_user_question is None
    assert state.pending_user_choices == ()
    assert state.last_turn_id == 12
    assert state.last_turn_status == "completed"
    assert state.last_assistant_summary == "Finished the architecture review. Next step is context hardening."
    assert state.updated_by_turn_id == 12


def test_build_session_state_after_failed_turn_clears_last_assistant_summary() -> None:
    state = build_session_state_after_turn(
        ConversationSessionState(last_assistant_summary="previous answer"),
        turn_id=13,
        status="failed",
    )

    assert state.last_turn_id == 13
    assert state.last_turn_status == "failed"
    assert state.last_assistant_summary is None
