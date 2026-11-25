from open_webui.routers.openai import convert_to_responses_payload


def test_reasoning_effort_moves_under_reasoning():
    out = convert_to_responses_payload(
        {'model': 'm', 'reasoning_effort': 'high', 'messages': [{'role': 'user', 'content': 'hi'}]}
    )
    assert 'reasoning_effort' not in out
    assert out['reasoning'] == {'effort': 'high'}


def test_existing_reasoning_object_kept():
    out = convert_to_responses_payload(
        {
            'model': 'm',
            'reasoning_effort': 'low',
            'reasoning': {'summary': 'auto'},
            'messages': [{'role': 'user', 'content': 'hi'}],
        }
    )
    assert out['reasoning'] == {'summary': 'auto', 'effort': 'low'}


def test_no_effort_no_reasoning():
    out = convert_to_responses_payload({'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]})
    assert 'reasoning' not in out
