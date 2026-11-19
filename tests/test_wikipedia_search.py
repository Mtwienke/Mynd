from unittest.mock import MagicMock

import httpx
import pytest

from mynd.connect.web_wikipedia import WikipediaSearch, WikipediaSearchError


def page(title, index, **extra):
    return {
        "ns": 0, "title": title, "index": index,
        "fullurl": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_"),
        **extra,
    }


def test_wikipedia_uses_full_text_search_and_returns_ranked_article_urls_not_snippets(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "must-not-leave-this-process")
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url).startswith("https://en.wikipedia.org/w/api.php?")
        assert request.url.params["generator"] == "search"
        assert request.url.params["gsrsearch"] == "Metaheuristic Algorithms"
        assert request.url.params["gsrnamespace"] == "0"
        assert request.url.params["gsrlimit"] == "5"
        assert request.url.params["maxlag"] == "5"
        assert request.url.params["ppprop"] == "disambiguation"
        assert request.headers["user-agent"] == "Mynd/0.1"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"query": {"pages": [
            page("Table of metaheuristics", 2, snippet="Never use this as a source"),
            page("Metaheuristic", 1),
            page("Disambiguation", 0, pageprops={"disambiguation": ""}),
            page("Talk:Metaheuristic", 3, ns=1),
            page("Missing", 4, missing=True),
            page("Unsafe", 5, fullurl="http://127.0.0.1/private"),
            page("Other host", 6, fullurl="https://other.example/article"),
        ]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = WikipediaSearch(client).search("Metaheuristic Algorithms definition overview")
    assert len(calls) == 1
    assert result == [
        {"href": "https://en.wikipedia.org/wiki/Metaheuristic", "title": "Metaheuristic"},
    ]


@pytest.mark.parametrize("query", ["Metaheuristic Algorithms", "Why do metaheuristic algorithms work?"])
def test_wikipedia_preserves_custom_queries_except_the_default_suffix(query):
    def handler(request):
        assert request.url.params["gsrsearch"] == query
        return httpx.Response(200, json={"batchcomplete": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert WikipediaSearch(client).search(query) == []


def test_wikipedia_filters_unrelated_biographies_and_matches_topic_acronyms():
    def handler(request):
        return httpx.Response(200, json={"query": {"pages": [
            page("Some Researcher", 1),
            page("Large language model", 2),
            page("Another Biography", 3),
        ]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = WikipediaSearch(client).search("LLM Research Frameworks definition overview")
    assert result == [{
        "href": "https://en.wikipedia.org/wiki/Large_language_model", "title": "Large language model",
    }]


@pytest.mark.parametrize(("query", "titles"), [
    ("Surrogate-Assisted Evolutionary Techniques",
     ["Religious response to assisted reproductive technology"]),
    ("Power System Optimization", ["Power optimization (EDA)", "Program optimization"]),
])
def test_wikipedia_does_not_hide_search_misses_with_weak_or_wrong_sense_matches(query, titles):
    def handler(request):
        return httpx.Response(200, json={"query": {"pages": [
            page(title, index) for index, title in enumerate(titles)
        ]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert WikipediaSearch(client).search(query) == []


@pytest.mark.parametrize("payload", [
    {"batchcomplete": True},
    {"query": {"pages": []}},
])
def test_wikipedia_success_without_matches_is_not_a_network_error(payload):
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)
    )) as client:
        assert WikipediaSearch(client).search("No matching concept") == []


@pytest.mark.parametrize(("status", "expected"), [
    (429, "rate limited.*HTTP 429"),
    (403, "HTTP 403"),
    (503, "HTTP 503"),
    (302, "HTTP 302"),
])
def test_wikipedia_reports_http_errors_without_echoing_remote_bodies(status, expected):
    def handler(request):
        return httpx.Response(status, text="Never echo secret-key or this body",
                              headers={"location": "http://127.0.0.1/private"})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(WikipediaSearchError, match=expected) as error,
    ):
        WikipediaSearch(client).search("Metaheuristic")
    assert "secret-key" not in str(error.value) and "this body" not in str(error.value)


@pytest.mark.parametrize("code", ["maxlag", "ratelimited"])
def test_wikipedia_reports_api_level_busy_status_even_with_http_200(code):
    def handler(request):
        return httpx.Response(200, json={"error": {"code": code, "info": "Never echo"}})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(WikipediaSearchError, match="busy or rate limited") as error,
    ):
        WikipediaSearch(client).search("Metaheuristic")
    assert "Never echo" not in str(error.value)


@pytest.mark.parametrize(("failure", "expected"), [
    (httpx.ReadTimeout("sensitive details"), "timed out"),
    (httpx.ConnectError("sensitive details"), "network request failed"),
])
def test_wikipedia_distinguishes_network_errors_from_no_results(failure, expected):
    def handler(request):
        raise failure

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(WikipediaSearchError, match=expected) as error,
    ):
        WikipediaSearch(client).search("Metaheuristic")
    assert "sensitive details" not in str(error.value)


@pytest.mark.parametrize("payload", [
    [], {"query": []}, {"query": {"pages": {}}},
    {"query": {"pages": [page("Metaheuristic", 1, pageprops=None)]}},
    {"error": {"code": "unknown", "info": "Do not echo"}},
])
def test_wikipedia_refuses_malformed_api_responses(payload):
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)
    )) as client, pytest.raises(WikipediaSearchError):
        WikipediaSearch(client).search("Metaheuristic")


def test_wikipedia_rejects_invalid_json():
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="Not JSON")
    )) as client, pytest.raises(WikipediaSearchError, match="invalid JSON"):
        WikipediaSearch(client).search("Metaheuristic")


def test_wikipedia_bounds_api_response_size(monkeypatch):
    monkeypatch.setattr("mynd.connect.web_wikipedia.MAX_RESPONSE_BYTES", 20)
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="x" * 100)
    )) as client, pytest.raises(WikipediaSearchError, match="size limit"):
        WikipediaSearch(client).search("Metaheuristic")


def test_empty_query_does_not_make_a_request_and_client_is_closed():
    client = MagicMock()
    search = WikipediaSearch(client)
    assert search.search("  ") == []
    client.stream.assert_not_called()
    search.close()
    client.close.assert_called_once()
