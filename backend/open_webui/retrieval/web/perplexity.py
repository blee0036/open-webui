import logging
from typing import Literal, Optional

import requests
from fastapi import HTTPException
from open_webui.env import VERSION
from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
from open_webui.retrieval.web.main import SearchResult, get_filtered_results
from open_webui.utils.quota_transport import metered_request
from open_webui.utils.session_pool import get_session

MODELS = Literal[
    'sonar',
    'sonar-pro',
    'sonar-reasoning',
    'sonar-reasoning-pro',
    'sonar-deep-research',
]
SEARCH_CONTEXT_USAGE_LEVELS = Literal['low', 'medium', 'high']


log = logging.getLogger(__name__)
API_URL = 'https://api.perplexity.ai/chat/completions'


def _payload(query, model, search_context_usage):
    return {
        'model': model,
        'messages': [
            {'role': 'system', 'content': 'You are a search assistant. Provide factual information with citations.'},
            {'role': 'user', 'content': query},
        ],
        'temperature': 0.2,
        'stream': False,
        'web_search_options': {'search_context_usage': search_context_usage},
    }


def _headers(api_key):
    return {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'X-Pplx-Integration': f'open-webui/{VERSION}',
    }


def _results(data, count, filter_list):
    results = []
    for i, citation in enumerate(data.get('citations', [])[:count]):
        content = data['choices'][0]['message']['content'] if i == 0 and data.get('choices') else ''
        results.append({'link': citation, 'title': f'Source {i + 1}', 'snippet': content})
    if filter_list:
        results = get_filtered_results(results, filter_list)
    return [SearchResult(**result) for result in results[:count]]


async def search_perplexity_metered(
    request, user, api_key, query, count, filter_list=None, model='sonar', search_context_usage='medium'
):
    """Sonar search is inference, unlike SearXNG or the Perplexity search-only API."""
    try:
        async with metered_request(
            await get_session(), request, user, model_id=model, url=API_URL,
            json=_payload(query, model, search_context_usage), headers=_headers(api_key),
            ssl=AIOHTTP_CLIENT_SESSION_SSL,
        ) as response:
            response.raise_for_status()
            return _results(await response.json(), count, filter_list)
    except HTTPException:
        raise  # In particular, never turn an exhausted quota into empty search results.
    except Exception:
        log.exception('Error searching with Perplexity API')
        return []


def search_perplexity(
    api_key: str,
    query: str,
    count: int,
    filter_list: Optional[list[str]] = None,
    model: MODELS = 'sonar',
    search_context_usage: SEARCH_CONTEXT_USAGE_LEVELS = 'medium',
) -> list[SearchResult]:
    """Search using Perplexity API and return the results as a list of SearchResult objects.

    Args:
      api_key (str): A Perplexity API key
      query (str): The query to search for
      count (int): Maximum number of results to return
      filter_list (Optional[list[str]]): List of domains to filter results
      model (str): The Perplexity model to use (sonar, sonar-pro)
      search_context_usage (str): Search context usage level (low, medium, high)

    """

    api_key = str(api_key)

    try:
        url = API_URL

        # Create payload for the API call
        payload = _payload(query, model, search_context_usage)

        headers = _headers(api_key)

        # Make the API request
        response = requests.request('POST', url, json=payload, headers=headers)

        # Parse the JSON response
        json_response = response.json()

        return _results(json_response, count, filter_list)

    except Exception as e:
        log.error(f'Error searching with Perplexity API: {e}')
        return []
