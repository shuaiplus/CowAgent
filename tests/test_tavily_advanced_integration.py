"""Tavily search defaults and Extract routing, without paid requests."""
import json
from unittest.mock import Mock

import pytest
import requests

from agent.tools.web_fetch import web_fetch as wf
from agent.tools.web_search import web_search as ws
from agent.tools.utils.url_safety import strict_url_guard


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = json.dumps(payload if payload is not None else {}).encode()
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


@pytest.fixture
def configured(monkeypatch):
    cfg = {'tools': {'web_fetch': {'provider': 'tavily'}, 'web_search': {
        'provider': 'tavily', 'strategy': 'fixed', 'tavily_api_key': 'fake-key',
        'tavily_search_depth': 'advanced'}}}
    monkeypatch.setattr(wf, 'conf', lambda: cfg)
    monkeypatch.setattr(ws, 'conf', lambda: cfg)
    monkeypatch.setattr(wf, 'validate_url_safe', lambda url: None)
    monkeypatch.setenv('WEB_SECURITY_SSRF_PROTECTION', 'false')
    return cfg


def test_search_schema_default_and_explicit_counts(configured, monkeypatch):
    captured = []
    def post(url, **kwargs):
        captured.append(kwargs['json'])
        r = Mock(status_code=200)
        r.json.return_value = {'results': []}
        return r
    monkeypatch.setattr(ws.requests, 'post', post)
    search = ws.WebSearch()
    schema = search.get_json_schema()['parameters']['properties']['count']
    assert 'default: 10' in schema['description']
    for value in (None, 5, 20, 30):
        args = {'query': 'test'}
        if value is not None:
            args['count'] = value
        assert search.execute(args).status == 'success'
    assert [x['max_results'] for x in captured] == [10, 5, 20, 20]
    assert all(x['search_depth'] == 'advanced' for x in captured)


def test_extract_success_and_key_reuse(configured, monkeypatch):
    response = Response(payload={'results': [{'raw_content': '# page\nbody'}], 'failed_results': []})
    post = Mock(return_value=response)
    monkeypatch.setattr(wf.requests, 'post', post)
    result = wf.WebFetch().execute({'url': 'https://example.com/article'})
    assert result.status == 'success'
    assert result.result == 'Title: Untitled\n\nContent:\n# page\nbody'
    args, kwargs = post.call_args
    assert args[0] == 'https://api.tavily.com/extract'
    assert kwargs['headers']['Authorization'] == 'Bearer fake-key'
    assert kwargs['json'] == {'urls': ['https://example.com/article'], 'extract_depth': 'advanced', 'format': 'markdown'}
    assert kwargs['stream'] is True and kwargs['allow_redirects'] is False
    assert response.closed


def test_document_is_local(configured, monkeypatch):
    post = Mock()
    monkeypatch.setattr(wf.requests, 'post', post)
    doc = Mock(return_value='document')
    monkeypatch.setattr(wf.WebFetch, '_fetch_document', doc)
    assert wf.WebFetch().execute({'url': 'https://example.com/file.pdf'}) == 'document'
    doc.assert_called_once()
    post.assert_not_called()


@pytest.mark.parametrize('status', [401, 402, 429, 432, 433, 500, 302])
def test_status_errors_never_fallback(configured, monkeypatch, status):
    response = Response(status=status)
    monkeypatch.setattr(wf.requests, 'post', Mock(return_value=response))
    direct = Mock()
    monkeypatch.setattr(wf.WebFetch, '_fetch_webpage', direct)
    assert wf.WebFetch().execute({'url': 'https://example.com/page'}).status == 'error'
    assert response.closed
    direct.assert_not_called()


@pytest.mark.parametrize('payload', [[], {}, {'results': []}, {'results': [{'raw_content': ''}]},
                                      {'results': [{'raw_content': 1}]},
                                      {'results': [], 'failed_results': [{'error': 'fake-key'}]}])
def test_bad_or_failed_payloads(configured, monkeypatch, payload):
    response = Response(payload=payload)
    monkeypatch.setattr(wf.requests, 'post', Mock(return_value=response))
    result = wf.WebFetch().execute({'url': 'https://example.com/page'})
    assert result.status == 'error' and 'fake-key' not in result.result
    assert response.closed


def test_size_limit_and_timeout(configured, monkeypatch):
    response = Response(payload={'results': [{'raw_content': 'abc' * 100}]})
    monkeypatch.setattr(wf, 'MAX_FILE_SIZE', 32)
    monkeypatch.setattr(wf.requests, 'post', Mock(return_value=response))
    assert 'too large' in wf.WebFetch().execute({'url': 'https://example.com/page'}).result
    assert response.closed
    monkeypatch.setattr(wf.requests, 'post', Mock(side_effect=requests.Timeout()))
    assert 'timed out' in wf.WebFetch().execute({'url': 'https://example.com/page'}).result


def test_guard_and_guest_allowlist_fail_closed(configured, monkeypatch):
    post = Mock()
    monkeypatch.setattr(wf.requests, 'post', post)
    with strict_url_guard(['example.com']):
        result = wf.WebFetch().execute({'url': 'https://example.com/page'})
    assert result.status == 'error' and 'redirect security' in result.result
    post.assert_not_called()


def test_missing_key(configured, monkeypatch):
    configured['tools']['web_search']['tavily_api_key'] = ''
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    post = Mock()
    monkeypatch.setattr(wf.requests, 'post', post)
    assert 'TAVILY_API_KEY' in wf.WebFetch().execute({'url': 'https://example.com/page'}).result
    post.assert_not_called()
