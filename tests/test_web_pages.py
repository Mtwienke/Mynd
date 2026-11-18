import socket

import httpx
import pytest

from mynd.connect.web_pages import PageFetcher, PageFetchError

TEXT = "Self-attention weights tokens using contextual information from the input sequence. " * 5


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    def resolve(host, port, *, type):
        return [(socket.AF_INET, type, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    monkeypatch.setattr("mynd.connect.web_pages.socket.getaddrinfo", resolve)


def mock_client(handler):
    def route(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(route))


def test_reader_extracts_main_content_and_pins_public_ip_without_credentials():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "example.org"
        assert request.extensions["sni_hostname"] == "example.org"
        assert "authorization" not in request.headers
        assert request.headers["connection"] == "close"
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            "<html><head><title>Attention reference</title></head><body>"
            "<nav>Navigation junk</nav><main><h1>Attention</h1>"
            f"<p>{TEXT}</p><script>Secret script junk</script></main>"
            "<footer>Footer junk</footer></body></html>"
        ))

    with mock_client(handler) as client:
        page = PageFetcher(client).fetch("https://example.org/attention")
    assert page["url"] == "https://example.org/attention"
    assert page["title"] == "Attention reference"
    assert TEXT.strip() in page["content"]
    assert "junk" not in page["content"]
    assert len(seen) == 1


@pytest.mark.parametrize("destination", [
    "http://127.0.0.1/private",
    "http://192.168.1.1/private",
    "file:///etc/passwd",
    "https://user:password@example.org/",
])
def test_reader_rejects_unsafe_redirects_before_requesting_them(destination):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": destination})

    with mock_client(handler) as client, pytest.raises(PageFetchError):
        PageFetcher(client).fetch("https://example.org/start")
    assert len(requests) == 1


def test_reader_checks_dns_for_redirect_hosts(monkeypatch):
    requests = []

    def resolve(host, port, *, type):
        address = "10.0.0.1" if host == "private.example" else "93.184.216.34"
        return [(socket.AF_INET, type, socket.IPPROTO_TCP, "", (address, port))]

    monkeypatch.setattr("mynd.connect.web_pages.socket.getaddrinfo", resolve)

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://private.example/secret"})

    with mock_client(handler) as client, pytest.raises(PageFetchError, match="non-public address"):
        PageFetcher(client).fetch("https://example.org/start")
    assert len(requests) == 1


@pytest.mark.parametrize("addresses", [
    ["127.0.0.1"], ["169.254.169.254"], ["::1"], ["93.184.216.34", "192.168.0.1"],
])
def test_reader_rejects_private_and_mixed_dns_without_network_requests(monkeypatch, addresses):
    monkeypatch.setattr("mynd.connect.web_pages.socket.getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))
        for address in addresses
    ])
    requests = []
    with (
        httpx.Client(transport=httpx.MockTransport(requests.append)) as client,
        pytest.raises(PageFetchError, match="non-public address"),
    ):
        PageFetcher(client).fetch("https://example.org/secret")
    assert not requests


def test_reader_returns_final_public_url_and_checks_each_origins_robots():
    requests = []

    def handler(request):
        requests.append((request.headers["host"], request.url.path))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "https://other.example/final"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text=TEXT)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        page = PageFetcher(client).fetch("https://example.org/start")
    assert page["url"] == "https://other.example/final"
    assert requests == [
        ("example.org", "/robots.txt"), ("example.org", "/start"),
        ("other.example", "/robots.txt"), ("other.example", "/final"),
    ]


def test_reader_never_forwards_cookies_between_pinned_virtual_hosts():
    requests = []

    def handler(request):
        assert "cookie" not in request.headers
        requests.append(request.url.path)
        headers = {"set-cookie": "private-session=do-not-forward; Path=/"}
        if request.url.path == "/robots.txt":
            return httpx.Response(404, headers=headers)
        if request.url.path == "/start":
            headers["location"] = "https://other.example/final"
            return httpx.Response(302, headers=headers)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text=TEXT)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        PageFetcher(client).fetch("https://example.org/start")
    assert requests == ["/robots.txt", "/start", "/robots.txt", "/final"]


@pytest.mark.parametrize(("status", "rules"), [
    (200, "User-agent: *\nDisallow: /private\n"),
    (403, ""),
])
def test_reader_honors_robots_denials_without_fetching_page(status, rules):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(status, text=rules)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(PageFetchError, match="robots.txt disallows"),
    ):
        PageFetcher(client).fetch("https://example.org/private")
    assert requests == ["/robots.txt"]


def test_reader_defers_on_robots_server_failure():
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(503)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        PageFetcher(client).fetch("https://example.org/page")
    assert requests == ["/robots.txt"]


@pytest.mark.parametrize("advertised", [False, True])
def test_reader_bounds_streamed_and_advertised_download_size(monkeypatch, advertised):
    monkeypatch.setattr("mynd.connect.web_pages.MAX_PAGE_BYTES", 80)

    def handler(request):
        response = httpx.Response(200, headers={"content-type": "text/plain"}, text=TEXT)
        if not advertised:
            del response.headers["content-length"]
        return response

    with mock_client(handler) as client, pytest.raises(PageFetchError, match="size limit"):
        PageFetcher(client).fetch("https://example.org/large")


@pytest.mark.parametrize(("content_type", "text", "error"), [
    ("application/pdf", TEXT, "not HTML or plain text"),
    ("text/html", "<html><body>Too short.</body></html>", "insufficient usable content"),
    ("text/html", "<title>Just a moment...</title>" + TEXT, "interactive browser"),
    ("text/html", "<main>" + TEXT + "<table id='disambigbox'></table></main>",
     "disambiguation index"),
])
def test_reader_rejects_unsupported_or_unusable_pages(content_type, text, error):
    def handler(request):
        return httpx.Response(200, headers={"content-type": content_type}, text=text)

    with mock_client(handler) as client, pytest.raises(PageFetchError, match=error):
        PageFetcher(client).fetch("https://example.org/page")


def test_reader_bounds_redirect_loops():
    def handler(request):
        return httpx.Response(302, headers={"location": "/again"})

    with mock_client(handler) as client, pytest.raises(PageFetchError, match="redirect limit"):
        PageFetcher(client).fetch("https://example.org/again")
