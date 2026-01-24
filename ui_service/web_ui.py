"""Web UI for browsing websites over Meshtastic mesh network.

This module provides a Flask-based web interface that allows users to browse
websites through the Meshtastic gateway. All HTTP requests are proxied through
the mesh network.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode

from flask import Flask, Response, request, render_template_string, jsonify, stream_with_context

import sys

ROOT = Path(__file__).resolve()
while ROOT != ROOT.parent and not (ROOT / "src").exists():
    ROOT = ROOT.parent
SRC = ROOT / "src"
if SRC.exists() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from client import MeshtasticClient
from radio import build_radio
from transport import MeshtasticTransport

LOGGER = logging.getLogger(__name__)


# HTML template for the web browser UI
BROWSER_HTML = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Meshtastic Web Browser</title>
    <style>
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        
        :root {
            --bg-dark: #0a0a0f;
            --bg-panel: #12121a;
            --bg-input: #1a1a25;
            --border-color: #2a2a3a;
            --text-primary: #e0e0e0;
            --text-secondary: #888;
            --accent-cyan: #00ffff;
            --accent-blue: #0066ff;
            --accent-gradient: linear-gradient(135deg, #00ffff, #0066ff);
            --error-red: #ff4444;
            --success-green: #44ff88;
        }
        
        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: var(--bg-dark);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        
        .header {
            background: var(--bg-panel);
            border-bottom: 1px solid var(--border-color);
            padding: 12px 20px;
            display: flex;
            align-items: center;
            gap: 20px;
        }
        
        .logo {
            font-size: 1.2em;
            font-weight: bold;
            background: var(--accent-gradient);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            white-space: nowrap;
        }
        
        .gateway-info {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.85em;
            color: var(--text-secondary);
        }
        
        .gateway-id {
            font-family: monospace;
            color: var(--accent-cyan);
        }
        
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--success-green);
        }
        
        .status-dot.disconnected {
            background: var(--error-red);
        }
        
        .url-bar-container {
            flex: 1;
            max-width: 800px;
        }
        
        .url-bar {
            display: flex;
            align-items: center;
            background: var(--bg-input);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 8px 12px;
            transition: border-color 0.2s;
        }
        
        .url-bar:focus-within {
            border-color: var(--accent-cyan);
        }
        
        .url-bar input {
            flex: 1;
            background: transparent;
            border: none;
            color: var(--text-primary);
            font-size: 14px;
            outline: none;
        }
        
        .url-bar input::placeholder {
            color: var(--text-secondary);
        }
        
        .url-bar button {
            background: var(--accent-gradient);
            border: none;
            color: #000;
            padding: 6px 16px;
            border-radius: 4px;
            cursor: pointer;
            font-weight: 600;
            margin-left: 8px;
            transition: opacity 0.2s;
        }
        
        .url-bar button:hover {
            opacity: 0.9;
        }
        
        .url-bar button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        
        .main-content {
            flex: 1;
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }
        
        .progress-bar-container {
            height: 3px;
            background: var(--bg-panel);
            overflow: hidden;
        }
        
        .progress-bar {
            height: 100%;
            background: var(--accent-gradient);
            width: 0%;
            transition: width 0.3s ease;
        }
        
        .progress-bar.indeterminate {
            width: 30%;
            animation: indeterminate 1.5s infinite linear;
        }
        
        @keyframes indeterminate {
            0% { transform: translateX(-100%); }
            100% { transform: translateX(400%); }
        }
        
        .status-bar {
            background: var(--bg-panel);
            border-bottom: 1px solid var(--border-color);
            padding: 8px 20px;
            font-size: 0.85em;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .status-text {
            color: var(--text-secondary);
        }
        
        .status-text.loading {
            color: var(--accent-cyan);
        }
        
        .status-text.error {
            color: var(--error-red);
        }
        
        .status-text.success {
            color: var(--success-green);
        }
        
        .transfer-stats {
            font-family: monospace;
            font-size: 0.8em;
            color: var(--text-secondary);
        }
        
        .browser-frame {
            flex: 1;
            background: white;
            margin: 0;
            overflow: auto;
        }
        
        .browser-frame iframe {
            width: 100%;
            height: 100%;
            border: none;
        }
        
        .welcome-screen {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            text-align: center;
            padding: 40px;
        }
        
        .welcome-icon {
            font-size: 4em;
            margin-bottom: 20px;
            opacity: 0.5;
        }
        
        .welcome-title {
            font-size: 1.5em;
            margin-bottom: 10px;
            color: var(--text-primary);
        }
        
        .welcome-text {
            color: var(--text-secondary);
            max-width: 500px;
            line-height: 1.6;
        }
        
        .welcome-tip {
            margin-top: 30px;
            padding: 15px 20px;
            background: var(--bg-panel);
            border-radius: 8px;
            border: 1px solid var(--border-color);
        }
        
        .welcome-tip strong {
            color: var(--accent-cyan);
        }
        
        .error-screen {
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            text-align: center;
            padding: 40px;
        }
        
        .error-icon {
            font-size: 3em;
            margin-bottom: 20px;
            color: var(--error-red);
        }
        
        .error-title {
            font-size: 1.3em;
            margin-bottom: 10px;
            color: var(--error-red);
        }
        
        .error-details {
            color: var(--text-secondary);
            max-width: 600px;
            line-height: 1.6;
            font-family: monospace;
            font-size: 0.9em;
            background: var(--bg-panel);
            padding: 15px;
            border-radius: 8px;
            margin-top: 15px;
            word-break: break-word;
        }
        
        .retry-button {
            margin-top: 20px;
            background: var(--accent-gradient);
            border: none;
            color: #000;
            padding: 10px 24px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
        }
    </style>
</head>
<body>
    <header class="header">
        <div class="logo">Meshtastic Browser</div>
        <div class="gateway-info">
            <span class="status-dot" id="statusDot"></span>
            <span>Gateway:</span>
            <span class="gateway-id" id="gatewayId">{{ gateway_id }}</span>
        </div>
        <div class="url-bar-container">
            <form class="url-bar" id="urlForm">
                <input type="text" id="urlInput" placeholder="Enter URL (e.g., example.com)" autocomplete="off" autofocus>
                <button type="submit" id="goButton">Go</button>
            </form>
        </div>
    </header>
    
    <div class="progress-bar-container">
        <div class="progress-bar" id="progressBar"></div>
    </div>
    
    <div class="status-bar" id="statusBar">
        <span class="status-text" id="statusText">Ready</span>
        <span class="transfer-stats" id="transferStats"></span>
    </div>
    
    <main class="main-content" id="mainContent">
        <div class="welcome-screen" id="welcomeScreen">
            <div class="welcome-icon">&#127760;</div>
            <h1 class="welcome-title">Browse the Web Over Meshtastic</h1>
            <p class="welcome-text">
                Enter a URL in the address bar above to fetch web pages through your Meshtastic mesh network.
                Pages are fetched via your gateway node and transmitted over radio.
            </p>
            <div class="welcome-tip">
                <strong>Tip:</strong> Simple text-based sites work best. Large images and complex pages may take longer to transfer.
            </div>
        </div>
    </main>
    
    <script>
        const urlInput = document.getElementById('urlInput');
        const urlForm = document.getElementById('urlForm');
        const goButton = document.getElementById('goButton');
        const progressBar = document.getElementById('progressBar');
        const statusText = document.getElementById('statusText');
        const transferStats = document.getElementById('transferStats');
        const mainContent = document.getElementById('mainContent');
        const welcomeScreen = document.getElementById('welcomeScreen');
        const statusDot = document.getElementById('statusDot');
        
        let currentUrl = '';
        let pollInterval = null;
        let requestId = null;
        
        // Normalize URL (add https:// if missing)
        function normalizeUrl(url) {
            url = url.trim();
            if (!url) return '';
            if (!url.match(/^https?:\\/\\//i)) {
                url = 'https://' + url;
            }
            return url;
        }
        
        // Update UI state
        function setLoading(loading) {
            goButton.disabled = loading;
            urlInput.disabled = loading;
            if (loading) {
                progressBar.classList.add('indeterminate');
                statusText.className = 'status-text loading';
            } else {
                progressBar.classList.remove('indeterminate');
                progressBar.style.width = '0%';
            }
        }
        
        function setStatus(text, type = '') {
            statusText.textContent = text;
            statusText.className = 'status-text ' + type;
        }
        
        function setProgress(percent) {
            progressBar.classList.remove('indeterminate');
            progressBar.style.width = percent + '%';
        }
        
        function setStats(stats) {
            transferStats.textContent = stats;
        }
        
        function showError(title, details) {
            mainContent.innerHTML = `
                <div class="error-screen">
                    <div class="error-icon">&#9888;</div>
                    <h2 class="error-title">${title}</h2>
                    <div class="error-details">${details}</div>
                    <button class="retry-button" onclick="retryLastRequest()">Retry</button>
                </div>
            `;
        }
        
        function showContent(html, baseUrl) {
            // Create iframe to display content
            mainContent.innerHTML = '<div class="browser-frame"><iframe id="contentFrame" sandbox="allow-same-origin"></iframe></div>';
            const iframe = document.getElementById('contentFrame');
            
            // Write content to iframe
            iframe.onload = function() {
                try {
                    // Intercept link clicks
                    const doc = iframe.contentDocument || iframe.contentWindow.document;
                    doc.addEventListener('click', function(e) {
                        const link = e.target.closest('a');
                        if (link && link.href) {
                            e.preventDefault();
                            // Navigate via our proxy
                            const href = link.getAttribute('href');
                            if (href && !href.startsWith('javascript:') && !href.startsWith('#')) {
                                let targetUrl = href;
                                if (!href.match(/^https?:\\/\\//i)) {
                                    targetUrl = new URL(href, baseUrl).href;
                                }
                                urlInput.value = targetUrl;
                                fetchUrl(targetUrl);
                            }
                        }
                    });
                } catch(e) {
                    console.log('Could not attach link handlers:', e);
                }
            };
            
            // Write the HTML content
            const doc = iframe.contentDocument || iframe.contentWindow.document;
            doc.open();
            doc.write(html);
            doc.close();
        }
        
        function retryLastRequest() {
            if (currentUrl) {
                fetchUrl(currentUrl);
            }
        }
        
        async function fetchUrl(url) {
            url = normalizeUrl(url);
            if (!url) return;
            
            currentUrl = url;
            urlInput.value = url;
            setLoading(true);
            setStatus('Sending request over mesh...', 'loading');
            setStats('');
            
            // Stop any existing polling
            if (pollInterval) {
                clearInterval(pollInterval);
                pollInterval = null;
            }
            
            try {
                // Start the request
                const response = await fetch('/api/browse', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: url })
                });
                
                const data = await response.json();
                
                if (data.error) {
                    throw new Error(data.error);
                }
                
                requestId = data.request_id;
                
                // Start polling for progress
                pollProgress();
                
            } catch (error) {
                setLoading(false);
                setStatus('Error: ' + error.message, 'error');
                showError('Request Failed', error.message);
            }
        }
        
        async function pollProgress() {
            if (!requestId) return;
            
            try {
                const response = await fetch('/api/status/' + requestId);
                const data = await response.json();
                
                if (data.status === 'sending') {
                    const sent = data.chunks_sent || 0;
                    const total = data.chunks_total || 1;
                    const percent = Math.round((sent / total) * 50);
                    setProgress(percent);
                    setStatus(`Sending request... (${sent}/${total} chunks)`, 'loading');
                    if (data.eta_seconds) {
                        setStats(`ETA: ${Math.round(data.eta_seconds)}s`);
                    }
                    setTimeout(pollProgress, 500);
                    
                } else if (data.status === 'receiving') {
                    const recv = data.chunks_received || 0;
                    const total = data.chunks_total || 1;
                    const percent = 50 + Math.round((recv / total) * 50);
                    setProgress(percent);
                    setStatus(`Receiving response... (${recv}/${total} chunks)`, 'loading');
                    if (data.eta_seconds) {
                        setStats(`ETA: ${Math.round(data.eta_seconds)}s`);
                    }
                    setTimeout(pollProgress, 500);
                    
                } else if (data.status === 'done') {
                    setProgress(100);
                    setLoading(false);
                    
                    if (data.error) {
                        setStatus('Error: ' + data.error, 'error');
                        showError('Request Failed', data.error);
                    } else {
                        const bytes = data.content_length || 0;
                        const duration = data.duration || 0;
                        setStatus(`Loaded (${formatBytes(bytes)} in ${duration.toFixed(1)}s)`, 'success');
                        setStats(`${Math.round(bytes/duration)} bytes/sec`);
                        
                        // Display the content
                        if (data.content_html) {
                            showContent(data.content_html, currentUrl);
                        } else if (data.content) {
                            showContent('<pre>' + escapeHtml(data.content) + '</pre>', currentUrl);
                        }
                    }
                    
                } else if (data.status === 'error') {
                    setLoading(false);
                    setStatus('Error: ' + (data.error || 'Unknown error'), 'error');
                    showError('Request Failed', data.error || 'Unknown error');
                    
                } else {
                    // Still processing
                    setTimeout(pollProgress, 500);
                }
                
            } catch (error) {
                console.error('Poll error:', error);
                setTimeout(pollProgress, 1000);
            }
        }
        
        function formatBytes(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        // Form submission
        urlForm.addEventListener('submit', function(e) {
            e.preventDefault();
            fetchUrl(urlInput.value);
        });
        
        // Check gateway connection
        async function checkConnection() {
            try {
                const response = await fetch('/api/health');
                const data = await response.json();
                statusDot.classList.toggle('disconnected', !data.connected);
            } catch (e) {
                statusDot.classList.add('disconnected');
            }
        }
        
        // Initial connection check
        checkConnection();
        setInterval(checkConnection, 5000);
    </script>
</body>
</html>
'''


@dataclass
class BrowseRequest:
    """Tracks state of an in-progress browse request."""
    request_id: str
    url: str
    status: str = "pending"  # pending, sending, receiving, done, error
    chunks_sent: int = 0
    chunks_total: int = 0
    chunks_received: int = 0
    recv_chunks_total: int = 0
    eta_seconds: float | None = None
    start_time: float = field(default_factory=time.time)
    content: str | None = None
    content_html: str | None = None
    content_length: int = 0
    http_status: int | None = None
    error: str | None = None
    duration: float = 0.0


class MeshWebBrowser:
    """Web browser that fetches pages over Meshtastic mesh network."""
    
    def __init__(
        self,
        gateway_node_id: str,
        transport: MeshtasticTransport | None = None,
        radio_port: str | None = None,
        host: str = "127.0.0.1",
        port: int = 8080,
    ):
        self.gateway_node_id = gateway_node_id
        self.host = host
        self.port = port
        self._transport = transport
        self._radio_port = radio_port
        self._radio = None
        self._client: MeshtasticClient | None = None
        
        # Track in-flight requests
        self._requests: dict[str, BrowseRequest] = {}
        self._request_lock = threading.Lock()
        self._request_counter = 0
        
        # Flask app
        self.app = Flask(__name__)
        self._setup_routes()
    
    def _setup_routes(self) -> None:
        """Configure Flask routes."""
        
        @self.app.route('/')
        def index():
            return render_template_string(BROWSER_HTML, gateway_id=self.gateway_node_id)
        
        @self.app.route('/api/health')
        def health():
            connected = self._transport is not None or self._radio is not None
            return jsonify({
                "connected": connected,
                "gateway_id": self.gateway_node_id,
            })
        
        @self.app.route('/api/browse', methods=['POST'])
        def browse():
            data = request.get_json()
            url = data.get('url', '').strip()
            
            if not url:
                return jsonify({"error": "URL is required"}), 400
            
            # Normalize URL
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
            
            # Create request tracking
            with self._request_lock:
                self._request_counter += 1
                request_id = f"req_{self._request_counter}_{int(time.time())}"
                browse_req = BrowseRequest(request_id=request_id, url=url)
                self._requests[request_id] = browse_req
            
            # Start fetch in background thread
            thread = threading.Thread(
                target=self._fetch_url,
                args=(request_id,),
                daemon=True,
            )
            thread.start()
            
            return jsonify({
                "request_id": request_id,
                "url": url,
                "status": "pending",
            })
        
        @self.app.route('/api/status/<request_id>')
        def status(request_id: str):
            with self._request_lock:
                req = self._requests.get(request_id)
                if not req:
                    return jsonify({"error": "Request not found"}), 404
                
                result = {
                    "request_id": req.request_id,
                    "url": req.url,
                    "status": req.status,
                    "chunks_sent": req.chunks_sent,
                    "chunks_total": req.chunks_total,
                    "chunks_received": req.chunks_received,
                }
                
                if req.status in ("sending",):
                    result["eta_seconds"] = req.eta_seconds
                elif req.status == "receiving":
                    result["chunks_total"] = req.recv_chunks_total
                    result["eta_seconds"] = req.eta_seconds
                elif req.status == "done":
                    result["content"] = req.content
                    result["content_html"] = req.content_html
                    result["content_length"] = req.content_length
                    result["http_status"] = req.http_status
                    result["duration"] = req.duration
                elif req.status == "error":
                    result["error"] = req.error
                
                return jsonify(result)
    
    def _ensure_client(self) -> MeshtasticClient:
        """Ensure we have a working client connection."""
        if self._client is not None:
            return self._client
        
        if self._transport is None:
            # Build our own radio/transport
            self._radio = build_radio(False, self._radio_port, "web_browser")
            self._transport = MeshtasticTransport(self._radio)
        
        self._client = MeshtasticClient(self._transport, self.gateway_node_id)
        return self._client
    
    def _fetch_url(self, request_id: str) -> None:
        """Fetch a URL over the mesh network."""
        with self._request_lock:
            req = self._requests.get(request_id)
            if not req:
                return
            req.status = "sending"
        
        start_time = time.time()
        
        def progress_callback(update: dict) -> None:
            with self._request_lock:
                req = self._requests.get(request_id)
                if not req:
                    return
                
                phase = update.get("phase", "")
                if phase == "send":
                    req.status = "sending"
                    req.chunks_sent = update.get("sent_chunks", 0)
                    req.chunks_total = update.get("total_chunks", 0)
                    req.eta_seconds = update.get("eta_seconds")
                elif phase == "receive":
                    req.status = "receiving"
                    req.chunks_received = update.get("received_chunks", 0)
                    req.recv_chunks_total = update.get("total_chunks", 0)
                    req.eta_seconds = update.get("eta_seconds")
        
        try:
            client = self._ensure_client()
            
            # Make the HTTP request through the gateway
            response = client.http_request(
                url=req.url,
                progress_callback=progress_callback,
                timeout=300.0,  # Long timeout for slow mesh
            )
            
            duration = time.time() - start_time
            
            with self._request_lock:
                req = self._requests.get(request_id)
                if not req:
                    return
                
                req.duration = duration
                
                if response.type == "error":
                    req.status = "error"
                    if isinstance(response.data, dict):
                        req.error = response.data.get("error", "Gateway returned error")
                    else:
                        req.error = "Gateway returned error"
                    return
                
                # Extract response data
                result = response.data.get("result", {}) if isinstance(response.data, dict) else {}
                
                req.http_status = result.get("status")
                req.content_length = result.get("content_length", 0)
                
                # Decode content
                content_b64 = result.get("content_b64", "")
                if content_b64:
                    try:
                        content_bytes = base64.b64decode(content_b64)
                        req.content = content_bytes.decode("utf-8", errors="replace")
                        req.content_length = len(content_bytes)
                    except Exception as e:
                        req.content = f"[Binary content, {len(content_b64)} bytes base64]"
                
                # Process HTML content - rewrite URLs to go through our proxy
                if req.content:
                    req.content_html = self._rewrite_html(req.content, req.url)
                
                req.status = "done"
                
        except TimeoutError as e:
            with self._request_lock:
                req = self._requests.get(request_id)
                if req:
                    req.status = "error"
                    req.error = f"Request timed out: {e}"
                    req.duration = time.time() - start_time
        except Exception as e:
            LOGGER.exception("Error fetching URL")
            with self._request_lock:
                req = self._requests.get(request_id)
                if req:
                    req.status = "error"
                    req.error = str(e)
                    req.duration = time.time() - start_time
    
    def _rewrite_html(self, html: str, base_url: str) -> str:
        """Rewrite HTML to make relative URLs absolute."""
        # Add base tag for relative URLs
        if '<head' in html.lower():
            html = re.sub(
                r'(<head[^>]*>)',
                rf'\1<base href="{base_url}">',
                html,
                count=1,
                flags=re.IGNORECASE
            )
        elif '<html' in html.lower():
            html = re.sub(
                r'(<html[^>]*>)',
                rf'\1<head><base href="{base_url}"></head>',
                html,
                count=1,
                flags=re.IGNORECASE
            )
        else:
            html = f'<head><base href="{base_url}"></head>' + html
        
        return html
    
    def run(self, debug: bool = False) -> None:
        """Start the web server."""
        LOGGER.info(f"Starting Meshtastic Web Browser on http://{self.host}:{self.port}")
        LOGGER.info(f"Gateway Node ID: {self.gateway_node_id}")
        self.app.run(host=self.host, port=self.port, debug=debug, threaded=True)
    
    def run_threaded(self) -> threading.Thread:
        """Start the web server in a background thread."""
        thread = threading.Thread(
            target=lambda: self.app.run(
                host=self.host, 
                port=self.port, 
                debug=False, 
                threaded=True,
                use_reloader=False,
            ),
            daemon=True,
            name="mesh-web-browser",
        )
        thread.start()
        LOGGER.info(f"Meshtastic Web Browser running on http://{self.host}:{self.port}")
        return thread
    
    def shutdown(self) -> None:
        """Clean up resources."""
        if self._radio and hasattr(self._radio, "close"):
            self._radio.close()


def main():
    """CLI entry point for the web browser."""
    import argparse
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    parser = argparse.ArgumentParser(description="Meshtastic Web Browser")
    parser.add_argument(
        "--gateway-node-id",
        required=True,
        help="Meshtastic node ID of the gateway",
    )
    parser.add_argument(
        "--radio-port",
        help="Serial port for the Meshtastic radio",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind the web server (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for the web server (default: 8080)",
    )
    
    args = parser.parse_args()
    
    browser = MeshWebBrowser(
        gateway_node_id=args.gateway_node_id,
        radio_port=args.radio_port,
        host=args.host,
        port=args.port,
    )
    
    try:
        browser.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        browser.shutdown()


if __name__ == "__main__":
    main()
