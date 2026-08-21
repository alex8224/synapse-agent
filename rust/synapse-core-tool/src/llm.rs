//! OpenAI-compatible chat completions client exposed to Python.
//!
//! Thin JSON-in/JSON-out bridge built on ``async-openai``'s ``byot`` feature:
//! requests and responses are ``serde_json::Value`` so non-standard fields
//! (DeepSeek ``reasoning_content``, ``extra_body``) pass through unchanged,
//! while async-openai still owns HTTP transport, SSE parsing, and config.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use async_openai::config::OpenAIConfig;
use async_openai::types::responses::ResponseWebSocketEvent;
use async_openai::{Client, ResponsesWebSocketOptions, WebSocketProxy};
use futures_util::StreamExt;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

fn runtime() -> &'static tokio::runtime::Runtime {
    static RT: OnceLock<tokio::runtime::Runtime> = OnceLock::new();
    RT.get_or_init(|| tokio::runtime::Runtime::new().expect("failed to start tokio runtime"))
}

/// A blocking Python iterator that drains JSON chunks from a tokio channel.
#[pyclass]
struct StreamIter {
    rx: Option<tokio::sync::mpsc::Receiver<Result<String, String>>>,
}

#[pymethods]
impl StreamIter {
    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(mut slf: PyRefMut<'_, Self>, py: Python<'_>) -> PyResult<Option<String>> {
        let Some(rx) = slf.rx.as_mut() else {
            return Ok(None);
        };
        match py.allow_threads(|| rx.blocking_recv()) {
            Some(Ok(json)) => Ok(Some(json)),
            Some(Err(e)) => Err(PyRuntimeError::new_err(e)),
            None => Ok(None),
        }
    }
}

async fn websocket_loop(
    ws: async_openai::ResponsesWebSocket<'_, OpenAIConfig>,
    cmd_rx: tokio::sync::mpsc::Receiver<WsCommand>,
    cache: Arc<Mutex<Option<Arc<WebSocketConnection>>>>,
    connection: Arc<WebSocketConnection>,
) {
    websocket_loop_inner(ws, cmd_rx).await;
    if let Ok(mut cached) = cache.lock() {
        if cached
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, &connection))
        {
            cached.take();
        }
    }
}

/// OpenAI-compatible chat client (raw JSON transport via async-openai byot).
///
/// The underlying ``async_openai::Client`` (and its reqwest connection pool) is
/// built once and reused across calls, so keep-alive connections and TLS
/// sessions persist between requests — mirroring the Python integration's
/// per-model cached httpx client.
#[pyclass]
struct RustOpenAIClient {
    client: async_openai::Client<OpenAIConfig>,
    websocket: Arc<Mutex<Option<Arc<WebSocketConnection>>>>,
    websocket_connect_lock: Arc<Mutex<()>>,
}

fn build_config(
    api_key: Option<&str>,
    base_url: Option<&str>,
    headers: &HashMap<String, String>,
) -> Result<OpenAIConfig, String> {
    let mut cfg = OpenAIConfig::default();
    // Explicitly override the env-derived key (OpenAIConfig::default reads
    // OPENAI_API_KEY) so credentials never leak to a custom base_url.
    cfg = cfg.with_api_key(api_key.unwrap_or_default().to_string());
    if let Some(base) = base_url {
        cfg = cfg.with_api_base(base);
    }
    for (k, v) in headers {
        let name = reqwest::header::HeaderName::from_bytes(k.as_bytes())
            .map_err(|e| format!("invalid header name {k:?}: {e}"))?;
        cfg = cfg
            .with_header(name, v.clone())
            .map_err(|e| format!("invalid header {k:?}: {e}"))?;
    }
    Ok(cfg)
}

/// Flatten a std::error::Error and its source chain into one readable string.
///
/// async-openai's ``OpenAIError::Reqwest`` display only shows the top-level
/// ``http error: {reqwest}`` text, which hides the underlying connect/TLS/DNS
/// cause. Walking the source chain keeps diagnostics actionable in Python.
fn format_error_chain(err: &(dyn std::error::Error + 'static)) -> String {
    let mut out = err.to_string();
    let mut source = err.source();
    while let Some(cause) = source {
        out.push_str(" => ");
        out.push_str(&cause.to_string());
        source = cause.source();
    }
    out
}

/// Build a reqwest client, optionally routing every request through a proxy.
///
/// The ``Client::with_config`` path uses a bare ``reqwest::Client::new()`` with
/// no proxy configured, so when a proxy URL is supplied we build the client
/// ourselves and inject it via ``Client::with_http_client``. Accepts the same
/// schemes as ``reqwest::Proxy`` (http, https, socks4/4a/5/5h, ``all``, ...).
fn build_http_client(proxy: Option<&str>) -> Result<reqwest::Client, String> {
    let mut builder = reqwest::Client::builder();
    if let Some(url) = proxy.filter(|p| !p.trim().is_empty()) {
        let configured =
            reqwest::Proxy::all(url).map_err(|e| format!("invalid proxy url {url:?}: {e}"))?;
        builder = builder.proxy(configured);
    }
    builder
        .build()
        .map_err(|e| format!("failed to build http client: {e}"))
}

/// Parse a ``socks5://host:port`` / ``socks5h://host:port`` URL into the fork's
/// WebSocket proxy config. Only SOCKS5 is supported by the WebSocket transport;
/// HTTP proxies are ignored (fall back to a direct connection).
fn parse_socks5_proxy(proxy: &str) -> Result<WebSocketProxy, String> {
    let raw = proxy.trim();
    let rest = if let Some(r) = raw.strip_prefix("socks5h://") {
        r
    } else if let Some(r) = raw.strip_prefix("socks5://") {
        r
    } else {
        return Err(format!(
            "websocket proxy must be socks5:// or socks5h://, got {proxy:?}"
        ));
    };
    let (host, port) = rest
        .rsplit_once(':')
        .ok_or_else(|| format!("websocket proxy must be host:port, got {proxy:?}"))?;
    let port: u16 = port
        .parse()
        .map_err(|_| format!("invalid websocket proxy port in {proxy:?}"))?;
    if host.is_empty() {
        return Err(format!("empty websocket proxy host in {proxy:?}"));
    }
    Ok(WebSocketProxy::Socks5 {
        host: host.to_string(),
        port,
    })
}

/// Serialize a WebSocket event to the JSON shape the Python stream converter
/// already understands (SSE-shaped ``response.*`` events, plus the nested WS
/// ``error`` structure). Returns ``(json, is_terminal)``.
fn websocket_event_to_json(event: ResponseWebSocketEvent) -> (String, bool) {
    match event {
        ResponseWebSocketEvent::Stream {
            stream_id: _,
            event,
        } => {
            let value = serde_json::to_value(&event).unwrap_or_default();
            let is_terminal = matches!(
                value.get("type").and_then(|v| v.as_str()),
                Some("response.completed") | Some("response.failed") | Some("response.incomplete")
            );
            (value.to_string(), is_terminal)
        }
        ResponseWebSocketEvent::Error(error) => {
            let mut value = serde_json::Map::new();
            value.insert("type".into(), serde_json::Value::String("error".into()));
            let mut detail = serde_json::Map::new();
            if let Some(code) = &error.error.code {
                detail.insert("code".into(), code.clone().into());
            }
            detail.insert("message".into(), error.error.message.clone().into());
            if let Some(param) = &error.error.param {
                detail.insert("param".into(), param.clone().into());
            }
            detail.insert("type".into(), error.error.r#type.clone().into());
            value.insert("error".into(), serde_json::Value::Object(detail));
            if let Some(sequence_number) = error.sequence_number {
                value.insert("sequence_number".into(), sequence_number.into());
            }
            (serde_json::Value::Object(value).to_string(), true)
        }
        ResponseWebSocketEvent::Unknown { data, .. } => {
            // Provider extension events (e.g. codex.rate_limits) are passed
            // through verbatim; the Python converter ignores unknown types.
            (data.to_string(), false)
        }
    }
}

/// Commands sent from Python to the background task that owns a persistent
/// WebSocket connection.
enum WsCommand {
    /// Send one ``response.create`` frame and stream its events back over ``tx``
    /// until the first terminal event.
    Request {
        data: String,
        tx: tokio::sync::mpsc::Sender<Result<String, String>>,
    },
    /// Close the socket and end the task.
    Close,
}

/// Background task owning one persistent Responses WebSocket connection.
///
/// Commands arrive serially: send a request, stream its events to the per-request
/// channel until a terminal event, then wait for the next command. Any send/recv
/// error (or a dropped request channel, e.g. Python cancellation) closes the
/// socket so unread events cannot leak into the next request.
async fn websocket_loop_inner(
    mut ws: async_openai::ResponsesWebSocket<'_, OpenAIConfig>,
    mut cmd_rx: tokio::sync::mpsc::Receiver<WsCommand>,
) {
    loop {
        let Some(cmd) = cmd_rx.recv().await else {
            let _ = ws.close().await;
            return;
        };
        match cmd {
            WsCommand::Request { data, tx } => {
                if let Err(e) = ws.send_raw(data).await {
                    let _ = tx
                        .send(Err(format!(
                            "websocket send failed: {}",
                            format_error_chain(&e)
                        )))
                        .await;
                    let _ = ws.close().await;
                    return;
                }
                loop {
                    match ws.recv().await {
                        Ok(event) => {
                            let (json, terminal) = websocket_event_to_json(event);
                            if tx.send(Ok(json)).await.is_err() {
                                // Python dropped the request (cancel/timeout);
                                // the socket may hold unread events, so close it.
                                let _ = ws.close().await;
                                return;
                            }
                            if terminal {
                                break;
                            }
                        }
                        Err(e) => {
                            let _ = tx
                                .send(Err(format!(
                                    "websocket recv failed: {}",
                                    format_error_chain(&e)
                                )))
                                .await;
                            let _ = ws.close().await;
                            return;
                        }
                    }
                }
            }
            WsCommand::Close => {
                let _ = ws.close().await;
                return;
            }
        }
    }
}

/// A handle to a persistent Responses WebSocket connection owned by a
/// background tokio task.
#[derive(Clone)]
struct WebSocketConnection {
    cmd_tx: tokio::sync::mpsc::Sender<WsCommand>,
}

impl WebSocketConnection {
    fn close(&self) {
        let _ = runtime().block_on(self.cmd_tx.send(WsCommand::Close));
    }
}

#[pyclass]
struct RustWebSocket {
    connection: Arc<WebSocketConnection>,
    cache: Arc<Mutex<Option<Arc<WebSocketConnection>>>>,
    connect_lock: Arc<Mutex<()>>,
}

#[pymethods]
impl RustWebSocket {
    /// Send one ``response.create`` frame and return an iterator over the raw
    /// JSON events of that response (ends after the first terminal event).
    fn request(&self, py: Python<'_>, request_json: String) -> PyResult<Py<StreamIter>> {
        let (tx, rx) = tokio::sync::mpsc::channel::<Result<String, String>>(64);
        let cmd = WsCommand::Request {
            data: request_json,
            tx,
        };
        py.allow_threads(|| runtime().block_on(self.connection.cmd_tx.send(cmd)))
            .map_err(|_| PyRuntimeError::new_err("websocket connection is closed"))?;
        Ok(Py::new(py, StreamIter { rx: Some(rx) })?)
    }

    /// Close the persistent connection.
    fn close(&self, py: Python<'_>) -> PyResult<()> {
        let cache = Arc::clone(&self.cache);
        let connection = Arc::clone(&self.connection);
        let connect_lock = Arc::clone(&self.connect_lock);
        py.allow_threads(move || {
            let _guard = connect_lock
                .lock()
                .map_err(|_| PyRuntimeError::new_err("websocket connect lock is poisoned"))?;
            let current = cache
                .lock()
                .map_err(|_| PyRuntimeError::new_err("websocket state lock is poisoned"))?
                .as_ref()
                .filter(|cached| Arc::ptr_eq(cached, &connection))
                .cloned();
            if let Some(current) = current {
                cache
                    .lock()
                    .map_err(|_| PyRuntimeError::new_err("websocket state lock is poisoned"))?
                    .take();
                current.close();
            }
            Ok(())
        })
    }
}

#[pymethods]
impl RustOpenAIClient {
    #[new]
    #[pyo3(signature = (api_key=None, base_url=None, headers=None, timeout_secs=None, proxy=None))]
    fn new(
        api_key: Option<String>,
        base_url: Option<String>,
        headers: Option<HashMap<String, String>>,
        timeout_secs: Option<f64>,
        proxy: Option<String>,
    ) -> PyResult<Self> {
        let _ = timeout_secs; // async-openai config does not expose a per-call timeout
        let base_url = base_url.filter(|b| !b.trim().is_empty());
        let api_key = api_key.filter(|k| !k.trim().is_empty());
        let headers = headers.unwrap_or_default();
        let cfg = build_config(api_key.as_deref(), base_url.as_deref(), &headers)
            .map_err(PyRuntimeError::new_err)?;
        let http_client = build_http_client(proxy.as_deref()).map_err(PyRuntimeError::new_err)?;
        Ok(Self {
            client: Client::with_config(cfg).with_http_client(http_client),
            websocket: Arc::new(Mutex::new(None)),
            websocket_connect_lock: Arc::new(Mutex::new(())),
        })
    }

    /// Send one non-streaming chat completion request, return the raw JSON body.
    fn complete(&self, py: Python<'_>, request_json: String) -> PyResult<String> {
        let request: serde_json::Value = serde_json::from_str(&request_json)
            .map_err(|e| PyRuntimeError::new_err(format!("invalid request JSON: {e}")))?;
        let resp: serde_json::Value = py
            .allow_threads(|| {
                runtime().block_on(async { self.client.chat().create_byot(request).await })
            })
            .map_err(|e| {
                PyRuntimeError::new_err(format!("request failed: {}", format_error_chain(&e)))
            })?;
        serde_json::to_string(&resp)
            .map_err(|e| PyRuntimeError::new_err(format!("serialize response failed: {e}")))
    }

    /// Send one non-streaming Responses API request, return the raw JSON body.
    fn complete_responses(&self, py: Python<'_>, request_json: String) -> PyResult<String> {
        let request: serde_json::Value = serde_json::from_str(&request_json)
            .map_err(|e| PyRuntimeError::new_err(format!("invalid request JSON: {e}")))?;
        let resp: serde_json::Value = py
            .allow_threads(|| {
                runtime().block_on(async { self.client.responses().create_byot(request).await })
            })
            .map_err(|e| {
                PyRuntimeError::new_err(format!("request failed: {}", format_error_chain(&e)))
            })?;
        serde_json::to_string(&resp)
            .map_err(|e| PyRuntimeError::new_err(format!("serialize response failed: {e}")))
    }

    /// Start a streaming chat completion; returns an iterator of raw JSON chunks.
    fn stream(&self, py: Python<'_>, request_json: String) -> PyResult<Py<StreamIter>> {
        let mut request: serde_json::Value = serde_json::from_str(&request_json)
            .map_err(|e| PyRuntimeError::new_err(format!("invalid request JSON: {e}")))?;
        request["stream"] = serde_json::Value::Bool(true);

        let (tx, rx) = tokio::sync::mpsc::channel::<Result<String, String>>(64);
        let client = self.client.clone();

        runtime().spawn(async move {
            let result = async {
                let mut stream = client
                    .chat()
                    .create_stream_byot::<_, serde_json::Value>(request)
                    .await
                    .map_err(|e| format!("request failed: {}", format_error_chain(&e)))?;
                while let Some(chunk) = stream.next().await {
                    let chunk = chunk.map_err(|e| format!("stream read failed: {e}"))?;
                    let json = serde_json::to_string(&chunk)
                        .map_err(|e| format!("serialize chunk failed: {e}"))?;
                    if tx.send(Ok(json)).await.is_err() {
                        return Ok(()); // Python side dropped the iterator
                    }
                }
                Ok(())
            }
            .await;
            if let Err(e) = result {
                let _ = tx.send(Err(e)).await;
            }
        });

        Ok(Py::new(py, StreamIter { rx: Some(rx) })?)
    }

    /// Start a streaming Responses API request; return an iterator of raw JSON events.
    fn stream_responses(&self, py: Python<'_>, request_json: String) -> PyResult<Py<StreamIter>> {
        let mut request: serde_json::Value = serde_json::from_str(&request_json)
            .map_err(|e| PyRuntimeError::new_err(format!("invalid request JSON: {e}")))?;
        request["stream"] = serde_json::Value::Bool(true);

        let (tx, rx) = tokio::sync::mpsc::channel::<Result<String, String>>(64);
        let client = self.client.clone();

        runtime().spawn(async move {
            let result = async {
                let mut stream = client
                    .responses()
                    .create_stream_byot::<_, serde_json::Value>(request)
                    .await
                    .map_err(|e| format!("request failed: {}", format_error_chain(&e)))?;
                while let Some(event) = stream.next().await {
                    let event = event.map_err(|e| format!("stream read failed: {e}"))?;
                    let json = serde_json::to_string(&event)
                        .map_err(|e| format!("serialize event failed: {e}"))?;
                    if tx.send(Ok(json)).await.is_err() {
                        return Ok(()); // Python side dropped the iterator
                    }
                }
                Ok(())
            }
            .await;
            if let Err(e) = result {
                let _ = tx.send(Err(e)).await;
            }
        });

        Ok(Py::new(py, StreamIter { rx: Some(rx) })?)
    }

    /// Open a persistent Responses WebSocket connection.
    ///
    /// Returns a [`RustWebSocket`] handle whose ``request`` method sends one
    /// ``response.create`` frame per call; the connection is reused across
    /// requests until closed or a socket error occurs. SOCKS5 proxies are
    /// applied to the handshake when supplied.
    #[pyo3(signature = (proxy=None))]
    fn open_websocket(&self, py: Python<'_>, proxy: Option<String>) -> PyResult<RustWebSocket> {
        let options = match proxy.as_deref().map(parse_socks5_proxy).transpose() {
            Ok(proxy) => {
                let mut opts = ResponsesWebSocketOptions::default();
                opts.proxy = proxy;
                // Disable the fork's transparent reconnect. A recovered
                // connection does not replay an in-flight `response.create`, so
                // `recv` could block forever on an empty socket. Surface the
                // disconnect to Python instead; the upper layer decides retry.
                opts.max_retries = 0;
                opts
            }
            Err(e) => return Err(PyRuntimeError::new_err(e)),
        };

        let client = self.client.clone();
        let cache = Arc::clone(&self.websocket);
        let connect_lock = Arc::clone(&self.websocket_connect_lock);
        let result: Result<Arc<WebSocketConnection>, String> = py.allow_threads(move || {
            let _connect_guard = connect_lock
                .lock()
                .map_err(|_| "websocket connect lock is poisoned".to_string())?;
            if let Some(connection) = cache
                .lock()
                .map_err(|_| "websocket state lock is poisoned".to_string())?
                .as_ref()
                .cloned()
            {
                if !connection.cmd_tx.is_closed() {
                    return Ok(connection);
                }
                cache
                    .lock()
                    .map_err(|_| "websocket state lock is poisoned".to_string())?
                    .take();
            }
            let (cmd_tx, cmd_rx) = tokio::sync::mpsc::channel::<WsCommand>(8);
            let (ready_tx, ready_rx) = tokio::sync::oneshot::channel::<Result<(), String>>();
            let connection = Arc::new(WebSocketConnection { cmd_tx });
            let task_connection = Arc::clone(&connection);
            let task_cache = Arc::clone(&cache);
            runtime().spawn(async move {
                let ws = client.responses().websocket_with_options(options).await;
                let ws = match ws {
                    Ok(ws) => ws,
                    Err(e) => {
                        let _ = ready_tx.send(Err(format!(
                            "websocket connect failed: {}",
                            format_error_chain(&e)
                        )));
                        return;
                    }
                };
                let _ = ready_tx.send(Ok(()));
                websocket_loop(ws, cmd_rx, task_cache, task_connection).await;
            });
            runtime()
                .block_on(ready_rx)
                .map_err(|_| "websocket task terminated unexpectedly".to_string())??;
            let mut cached = cache
                .lock()
                .map_err(|_| "websocket state lock is poisoned".to_string())?;
            if let Some(existing) = cached.as_ref().cloned() {
                drop(cached);
                connection.close();
                return Ok(existing);
            }
            *cached = Some(Arc::clone(&connection));
            Ok(connection)
        });
        let connection = result.map_err(PyRuntimeError::new_err)?;
        Ok(RustWebSocket {
            connection,
            cache: Arc::clone(&self.websocket),
            connect_lock: Arc::clone(&self.websocket_connect_lock),
        })
    }

    /// Close and forget the cached Responses WebSocket, if any.
    fn close_websocket(&self, py: Python<'_>) -> PyResult<()> {
        let cache = Arc::clone(&self.websocket);
        let connect_lock = Arc::clone(&self.websocket_connect_lock);
        py.allow_threads(move || {
            let _guard = connect_lock
                .lock()
                .map_err(|_| PyRuntimeError::new_err("websocket connect lock is poisoned"))?;
            let connection = cache
                .lock()
                .map_err(|_| PyRuntimeError::new_err("websocket state lock is poisoned"))?
                .take();
            if let Some(connection) = connection {
                connection.close();
            }
            Ok(())
        })
    }
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<RustOpenAIClient>()?;
    module.add_class::<RustWebSocket>()?;
    module.add_class::<StreamIter>()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_socks5_proxies() {
        let p = parse_socks5_proxy("socks5h://localhost:7991").unwrap();
        assert_eq!(
            p,
            WebSocketProxy::Socks5 {
                host: "localhost".into(),
                port: 7991
            }
        );
        let p = parse_socks5_proxy("socks5://127.0.0.1:1080").unwrap();
        assert_eq!(
            p,
            WebSocketProxy::Socks5 {
                host: "127.0.0.1".into(),
                port: 1080
            }
        );
        assert!(parse_socks5_proxy("http://localhost:7890").is_err());
        assert!(parse_socks5_proxy("socks5://localhost").is_err());
    }

    #[test]
    fn stream_event_to_json_is_not_terminal() {
        let event = ResponseWebSocketEvent::parse(
            r#"{"type":"response.output_text.delta","sequence_number":1,"item_id":"item_1","output_index":0,"content_index":0,"delta":"hi"}"#,
        )
        .unwrap();
        let (json, terminal) = websocket_event_to_json(event);
        assert!(!terminal);
        let value: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(value["type"], "response.output_text.delta");
        assert_eq!(value["delta"], "hi");
    }

    #[test]
    fn error_event_to_json_is_terminal_and_nested() {
        let event = ResponseWebSocketEvent::parse(
            r#"{"type":"error","sequence_number":5,"error":{"code":"invalid_request_error","message":"bad","param":"input","type":"invalid_request"}}"#,
        )
        .unwrap();
        let (json, terminal) = websocket_event_to_json(event);
        assert!(terminal);
        let value: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(value["type"], "error");
        assert_eq!(value["error"]["message"], "bad");
    }

    #[test]
    fn unknown_event_passes_through_and_is_not_terminal() {
        let event =
            ResponseWebSocketEvent::parse(r#"{"type":"codex.rate_limits","tokens":123}"#).unwrap();
        let (json, terminal) = websocket_event_to_json(event);
        assert!(!terminal);
        let value: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(value["type"], "codex.rate_limits");
        assert_eq!(value["tokens"], 123);
    }
}
