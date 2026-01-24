"""Unit tests for MeshWebBrowser web UI."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_SERVICE = ROOT / "ui_service"
if UI_SERVICE.exists() and str(UI_SERVICE) not in sys.path:
    sys.path.insert(0, str(UI_SERVICE))

from web_ui import MeshWebBrowser, BrowseRequest
from transport import InMemoryRadio, MeshtasticTransport


@pytest.fixture
def mock_transport():
    """Create a mock transport for testing."""
    radio = InMemoryRadio("test-node")
    return MeshtasticTransport(radio)


@pytest.fixture
def web_browser(mock_transport):
    """Create a MeshWebBrowser instance for testing."""
    browser = MeshWebBrowser(
        gateway_node_id="!test123",
        transport=mock_transport,
        host="127.0.0.1",
        port=8888,
    )
    return browser


def test_web_browser_initialization(web_browser):
    """Test MeshWebBrowser initialization."""
    assert web_browser.gateway_node_id == "!test123"
    assert web_browser.host == "127.0.0.1"
    assert web_browser.port == 8888
    assert web_browser._transport is not None
    assert web_browser._requests == {}
    assert web_browser._request_counter == 0


def test_browse_request_dataclass():
    """Test BrowseRequest dataclass creation."""
    req = BrowseRequest(request_id="req_1", url="https://example.com")
    assert req.request_id == "req_1"
    assert req.url == "https://example.com"
    assert req.status == "pending"
    assert req.chunks_sent == 0
    assert req.error is None


def test_api_browse_missing_url(web_browser):
    """Test /api/browse endpoint with missing URL."""
    with web_browser.app.test_client() as client:
        response = client.post('/api/browse', json={})
        assert response.status_code == 400
        data = response.get_json()
        assert "error" in data


def test_api_browse_invalid_json(web_browser):
    """Test /api/browse endpoint with invalid JSON."""
    with web_browser.app.test_client() as client:
        response = client.post(
            '/api/browse',
            data='invalid json',
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert "error" in data
        assert "Invalid or missing JSON body" in data["error"]


def test_api_browse_non_http_url(web_browser):
    """Test /api/browse endpoint with non-HTTP URL scheme."""
    with web_browser.app.test_client() as client:
        # Test javascript: protocol
        response = client.post('/api/browse', json={"url": "javascript:alert(1)"})
        assert response.status_code == 400
        data = response.get_json()
        assert "error" in data
        assert "Only http and https URLs are allowed" in data["error"]
        
        # Test file: protocol
        response = client.post('/api/browse', json={"url": "file:///etc/passwd"})
        assert response.status_code == 400
        data = response.get_json()
        assert "error" in data


def test_api_browse_url_normalization(web_browser):
    """Test /api/browse endpoint normalizes URLs correctly."""
    with web_browser.app.test_client() as client:
        response = client.post('/api/browse', json={"url": "example.com"})
        assert response.status_code == 200
        data = response.get_json()
        assert data["url"] == "https://example.com"


def test_api_browse_creates_request(web_browser):
    """Test /api/browse endpoint creates a browse request."""
    with web_browser.app.test_client() as client:
        response = client.post('/api/browse', json={"url": "https://example.com"})
        assert response.status_code == 200
        data = response.get_json()
        assert "request_id" in data
        assert data["url"] == "https://example.com"
        assert data["status"] == "pending"
        
        # Verify request was added to internal tracking
        with web_browser._request_lock:
            assert data["request_id"] in web_browser._requests


def test_api_status_nonexistent_request(web_browser):
    """Test /api/status endpoint with non-existent request."""
    with web_browser.app.test_client() as client:
        response = client.get('/api/status/nonexistent_id')
        assert response.status_code == 404


def test_api_status_existing_request(web_browser):
    """Test /api/status endpoint with existing request."""
    # Create a request manually
    with web_browser._request_lock:
        req = BrowseRequest(request_id="test_req", url="https://example.com")
        web_browser._requests["test_req"] = req
    
    with web_browser.app.test_client() as client:
        response = client.get('/api/status/test_req')
        assert response.status_code == 200
        data = response.get_json()
        assert data["request_id"] == "test_req"
        assert data["url"] == "https://example.com"
        assert data["status"] == "pending"


def test_requests_cleanup(web_browser):
    """Test that old completed requests are cleaned up."""
    # Add 101 completed requests to trigger cleanup
    with web_browser._request_lock:
        for i in range(101):
            req = BrowseRequest(
                request_id=f"req_{i}",
                url=f"https://example{i}.com",
            )
            req.status = "done"
            req.start_time = time.time() - (101 - i)  # Older requests have lower time
            web_browser._requests[f"req_{i}"] = req
    
    # Create a new request which should trigger cleanup
    with web_browser.app.test_client() as client:
        response = client.post('/api/browse', json={"url": "https://trigger-cleanup.com"})
        assert response.status_code == 200
    
    # Check that some old requests were cleaned up
    with web_browser._request_lock:
        # Should have cleaned up about half of the 101 completed requests
        # plus the new request, so total should be < 101
        assert len(web_browser._requests) < 101


def test_ensure_client_thread_safety(web_browser):
    """Test that _ensure_client is thread-safe."""
    clients = []
    errors = []
    
    def get_client():
        try:
            client = web_browser._ensure_client()
            clients.append(client)
        except Exception as e:
            errors.append(e)
    
    # Create multiple threads that try to get client simultaneously
    threads = [threading.Thread(target=get_client) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    # Check no errors occurred
    assert len(errors) == 0
    
    # All threads should get the same client instance
    assert len(set(id(c) for c in clients)) == 1


def test_rewrite_html_escapes_base_url(web_browser):
    """Test that _rewrite_html properly escapes base_url to prevent XSS."""
    # Try to inject script via base_url
    malicious_url = 'https://example.com" onload="alert(1)'
    html = "<html><body>Test</body></html>"
    
    result = web_browser._rewrite_html(html, malicious_url)
    
    # The URL should be HTML-escaped
    assert '&quot;' in result or 'https://example.com&quot;' in result
    # Should not contain the raw injection attempt
    assert 'onload="alert(1)"' not in result


def test_rewrite_html_adds_base_tag(web_browser):
    """Test that _rewrite_html adds base tag correctly."""
    html = "<html><head></head><body>Test</body></html>"
    base_url = "https://example.com/page"
    
    result = web_browser._rewrite_html(html, base_url)
    
    assert '<base href=' in result
    assert 'https://example.com/page' in result


def test_index_route(web_browser):
    """Test that the index route returns HTML."""
    with web_browser.app.test_client() as client:
        response = client.get('/')
        assert response.status_code == 200
        assert b'<!DOCTYPE html>' in response.data
        assert b'Meshtastic Web Browser' in response.data


def test_shutdown_closes_radio(web_browser):
    """Test that shutdown closes the radio connection."""
    # Create a mock radio
    mock_radio = Mock()
    mock_radio.close = Mock()
    web_browser._radio = mock_radio
    
    web_browser.shutdown()
    
    mock_radio.close.assert_called_once()


def test_shutdown_without_radio(web_browser):
    """Test that shutdown works when radio is None."""
    web_browser._radio = None
    web_browser.shutdown()  # Should not raise an exception
