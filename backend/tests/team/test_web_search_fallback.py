import pytest
from open_webui.routers import retrieval


@pytest.mark.asyncio
async def test_duckduckgo_falls_through_to_searxng(monkeypatch):
    calls = []
    outcome = {}

    async def engine(request, name, query, user=None, *, config=None):
        calls.append(name)
        result = outcome[name]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(retrieval, 'search_engine', engine)
    monkeypatch.setattr(retrieval, 'WEB_SEARCH_FALLBACK_ENGINE', 'searxng')
    config = {}

    outcome.update(duckduckgo=['ddg hit'], searxng=['searx hit'])
    assert await retrieval.search_web(None, 'duckduckgo', 'q', config=config) == ['ddg hit']
    assert calls == ['duckduckgo']

    for failure in (RuntimeError('202 Ratelimit'), []):
        calls.clear()
        outcome['duckduckgo'] = failure
        assert await retrieval.search_web(None, 'duckduckgo', 'q', config=config) == ['searx hit']
        assert calls == ['duckduckgo', 'searxng']

    # The fallback's own error surfaces; no loop when the engine is already the fallback.
    calls.clear()
    outcome['searxng'] = RuntimeError('searxng down')
    with pytest.raises(RuntimeError, match='searxng down'):
        await retrieval.search_web(None, 'searxng', 'q', config=config)
    assert calls == ['searxng']

    monkeypatch.setattr(retrieval, 'WEB_SEARCH_FALLBACK_ENGINE', '')
    calls.clear()
    outcome['duckduckgo'] = []
    assert await retrieval.search_web(None, 'duckduckgo', 'q', config=config) == []
    assert calls == ['duckduckgo']
