from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from mynd.connect.web import OllamaWeb, build_web_client
from mynd.connect.web_ddgs import SEARCH_BACKENDS, DDGSWeb
from mynd.connect.web_pages import PageFetchError
from mynd.connect.web_wikipedia import WikipediaSearchError

TEXT = "Fetched public explanation of attention and contextual token information. " * 5


@pytest.fixture(autouse=True)
def references(monkeypatch):
    # Unit tests never fall through to real Wikipedia requests.
    factory = MagicMock()
    factory.return_value.search.return_value = []
    monkeypatch.setattr("mynd.connect.web_ddgs.WikipediaSearch", factory)
    return factory.return_value


def test_ddgs_search_uses_only_explicit_engines_and_no_api_credentials(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "never-send-this-key")
    fetcher = MagicMock()
    fetcher.fetch.return_value = {
        "url": "https://example.org/attention", "title": "", "content": TEXT,
    }
    with patch("ddgs.DDGS") as search:
        search.return_value.__enter__.return_value.text.return_value = [
            {"href": "https://example.org/attention", "title": "Search title",
             "body": "A search snippet must not be the source."},
        ]
        web = DDGSWeb(fetcher)
        result = web.research("Self-attention definition overview")
    search.assert_called_once_with(timeout=10)
    search.return_value.__enter__.return_value.text.assert_called_once_with(
        "Self-attention definition overview", max_results=5, backend=SEARCH_BACKENDS,
    )
    assert result["provider"] == "ddgs"
    assert datetime.fromisoformat(result["fetched_at"]).tzinfo == UTC
    assert result["sources"][0]["text"] == TEXT
    assert result["sources"][0]["title"] == "Search title"
    assert result["sources"][0]["id"].startswith("web-")
    fetcher.fetch.assert_called_once_with("https://example.org/attention")
    web.close()
    fetcher.close.assert_called_once()


def test_ddgs_skips_unsafe_duplicate_and_failed_pages_and_caps_usable_sources(monkeypatch):
    fetcher = MagicMock()

    def fetch(url):
        if url.endswith("/blocked"):
            raise PageFetchError("robots.txt disallows this page")
        return {"url": url, "title": "Fetched title", "content": TEXT}

    fetcher.fetch.side_effect = fetch
    web = DDGSWeb(fetcher)
    monkeypatch.setattr(web, "_search", lambda query: [
        {"href": "http://127.0.0.1/secret"},
        {"href": "https://example.org/blocked"},
        {"href": "https://example.org/one"}, {"href": "https://example.org/one"},
        {"href": "https://example.org/two"},
        {"href": "https://example.org/outside-the-five-result-limit"},
    ])
    result = web.research("Attention")
    assert [source["url"] for source in result["sources"]] == [
        "https://example.org/one", "https://example.org/two",
    ]
    assert fetcher.fetch.call_count == 3

    fetcher.reset_mock()
    monkeypatch.setattr(web, "_search", lambda query: [
        {"href": f"https://example.org/{number}"} for number in range(5)
    ])
    assert len(web.research("Attention")["sources"]) == 3
    assert fetcher.fetch.call_count == 3


def test_ddgs_deduplicates_final_redirect_destinations(monkeypatch):
    fetcher = MagicMock()
    fetcher.fetch.return_value = {"url": "https://example.org/final", "title": "", "content": TEXT}
    web = DDGSWeb(fetcher)
    monkeypatch.setattr(web, "_search", lambda query: [
        {"href": "https://example.org/alias-one"}, {"href": "https://example.org/alias-two"},
    ])
    assert len(web.research("Attention")["sources"]) == 1


def test_ddgs_never_falls_back_to_search_snippets(monkeypatch):
    fetcher = MagicMock()
    fetcher.fetch.side_effect = PageFetchError("Page has insufficient usable content")
    web = DDGSWeb(fetcher)
    monkeypatch.setattr(web, "_search", lambda query: [
        {"href": "https://example.org/attention", "body": TEXT},
    ])
    with pytest.raises(ValueError, match="General search:.*insufficient usable content"):
        web.research("Attention")


def test_ddgs_reports_unavailable_search_without_echoing_response_bodies():
    with patch("ddgs.DDGS") as search:
        search.return_value.__enter__.return_value.text.side_effect = DDGSException(
            "arbitrary remote response including sensitive details"
        )
        with pytest.raises(ValueError, match="no results or engine refusal") as error:
            DDGSWeb(MagicMock()).research("Attention")
    assert "sensitive details" not in str(error.value)
    assert "no matching articles (not a rate-limit error)" in str(error.value)
    assert "--web-query TOPIC_ID=QUERY" in str(error.value)


def test_ddgs_uses_official_full_text_fallback_when_general_engines_fail(references):
    fetcher = MagicMock()
    fetcher.fetch.return_value = {
        "url": "https://en.wikipedia.org/wiki/Large_language_model",
        "title": "Large language model", "content": TEXT,
    }
    with patch("ddgs.DDGS") as search:
        text = search.return_value.__enter__.return_value.text
        text.side_effect = DDGSException("Blocked")
        references.search.return_value = [{"href": "https://en.wikipedia.org/wiki/Large_language_model"}]
        web = DDGSWeb(fetcher)
        result = web.research("Large language model definition overview")
    text.assert_called_once_with(
        "Large language model definition overview", max_results=5, backend=SEARCH_BACKENDS,
    )
    references.search.assert_called_once_with("Large language model definition overview")
    assert result["search_backend"] == "Wikipedia full-text API"
    assert result["sources"][0]["text"] == TEXT
    web.close()
    references.close.assert_called_once()


def test_ddgs_uses_reference_fallback_when_all_general_result_pages_fail(monkeypatch, references):
    fetcher = MagicMock()

    def fetch(url):
        if url == "https://example.org/blocked":
            raise PageFetchError("robots.txt disallows this page")
        return {"url": url, "title": "Metaheuristic", "content": TEXT}

    fetcher.fetch.side_effect = fetch
    references.search.return_value = [{"href": "https://en.wikipedia.org/wiki/Metaheuristic"}]
    web = DDGSWeb(fetcher)
    monkeypatch.setattr(web, "_search", lambda query: [{"href": "https://example.org/blocked"}])
    result = web.research("Metaheuristic Algorithms definition overview")
    assert result["sources"][0]["url"] == "https://en.wikipedia.org/wiki/Metaheuristic"
    assert result["search_backend"] == "Wikipedia full-text API"


@pytest.mark.parametrize(("error", "diagnostic"), [
    (TimeoutException("Never echo this"), "General search: timed out"),
    (RatelimitException("Never echo this"), "General search: rate limited"),
    (DDGSException("Never echo this"), "General search: no results or engine refusal"),
])
def test_ddgs_distinguishes_search_failure_types(error, diagnostic):
    with patch("ddgs.DDGS") as search:
        search.return_value.__enter__.return_value.text.side_effect = error
        with pytest.raises(ValueError, match=diagnostic) as caught:
            DDGSWeb(MagicMock()).research("Attention")
    assert "Never echo this" not in str(caught.value)


def test_ddgs_preserves_wikipedia_network_error_instead_of_calling_it_a_search_miss(references):
    references.search.side_effect = WikipediaSearchError("Wikipedia API network request failed")
    with patch("ddgs.DDGS") as search:
        search.return_value.__enter__.return_value.text.side_effect = DDGSException("No results")
        with pytest.raises(ValueError, match="Wikipedia API network request failed") as error:
            DDGSWeb(MagicMock()).research("Attention")
    assert "no matching articles" not in str(error.value)


def test_ddgs_invalid_search_results_still_allow_reference_fallback(monkeypatch, references):
    fetcher = MagicMock()
    fetcher.fetch.return_value = {
        "url": "https://en.wikipedia.org/wiki/Metaheuristic", "title": "Metaheuristic", "content": TEXT,
    }
    references.search.return_value = [{"href": "https://en.wikipedia.org/wiki/Metaheuristic"}]
    web = DDGSWeb(fetcher)
    monkeypatch.setattr(web, "_search", lambda query: None)
    assert web.research("Metaheuristic Algorithms")["sources"][0]["title"] == "Metaheuristic"


@pytest.mark.parametrize("results", [[], [None, {}, {"href": "file:///etc/passwd"}]])
def test_ddgs_empty_or_unsafe_results_do_not_fetch_pages(monkeypatch, results):
    fetcher = MagicMock()
    web = DDGSWeb(fetcher)
    monkeypatch.setattr(web, "_search", lambda query: results)
    with pytest.raises(ValueError, match="no usable public search results"):
        web.research("Attention")
    fetcher.fetch.assert_not_called()


def test_ddgs_summarizes_http_statuses_without_response_bodies(monkeypatch):
    fetcher = MagicMock()
    request = httpx.Request("GET", "https://example.org/blocked")
    response = httpx.Response(403, request=request, text="Never log this body")
    fetcher.fetch.side_effect = httpx.HTTPStatusError("Never log this body", request=request,
                                                   response=response)
    web = DDGSWeb(fetcher)
    monkeypatch.setattr(web, "_search", lambda query: [{"href": str(request.url)}])
    with pytest.raises(ValueError, match="HTTP 403") as error:
        web.research("Attention")
    assert "Never log" not in str(error.value)


def test_web_factory_selects_providers_without_making_requests():
    with patch("mynd.connect.web_ddgs.DDGSWeb") as ddgs:
        assert build_web_client("ddgs") is ddgs.return_value
    with patch("mynd.connect.web.OllamaWeb") as ollama:
        assert build_web_client("ollama") is ollama.return_value
    with pytest.raises(ValueError, match="Unknown web provider"):
        build_web_client("unsupported")


@pytest.mark.parametrize("endpoint", ["web_search", "web_fetch"])
def test_ollama_payment_error_is_actionable_and_not_repeated_per_topic(endpoint):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith(endpoint):
            return httpx.Response(402, json={"error": "Do not echo secret-key or this body"})
        return httpx.Response(200, json={"results": [{"url": "https://example.org/attention"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        web = OllamaWeb("secret-key", client)
        with pytest.raises(ValueError, match="requires usage credits.*HTTP 402") as error:
            web.research("Attention")
        assert "topic_web_provider = 'ddgs'" in str(error.value)
        assert "secret-key" not in str(error.value) and "this body" not in str(error.value)
        count = len(requests)
        with pytest.raises(ValueError, match="HTTP 402"):
            web.research("Another topic")
        assert len(requests) == count
