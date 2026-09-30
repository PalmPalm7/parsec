from src.connections import turn_state


def test_nothing_is_remembered_outside_a_turn():
    turn_state.mark_dead("aap2:prod0", "401")
    assert turn_state.dead_reason("aap2:prod0") is None


def test_a_turn_remembers_and_a_new_turn_forgets():
    token = turn_state.begin_turn()
    try:
        turn_state.mark_dead("aap2:prod0", "401")
        turn_state.mark_dead("aap2:prod0", "second reason is ignored")
        assert turn_state.dead_reason("aap2:prod0") == "401"
    finally:
        turn_state.end_turn(token)
    token = turn_state.begin_turn()
    try:
        assert turn_state.dead_reason("aap2:prod0") is None
    finally:
        turn_state.end_turn(token)
