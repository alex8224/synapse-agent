//! OpenAI-compatible chat completions client exposed to Python.
//!
//! Thin JSON-in/JSON-out bridge built on ``async-openai``'s ``byot`` feature:
//! requests and responses are ``serde_json::Value`` so non-standard fields
//! (DeepSeek ``reasoning_content``, ``extra_body``) pass through unchanged,
//! while async-openai still owns HTTP transport, SSE parsing, and config.

use std::collections::HashMap;
use std::sync::OnceLock;

use async_openai::config::OpenAIConfig;
use async_openai::Client;
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

/// OpenAI-compatible chat client (raw JSON transport via async-openai byot).
///
/// The underlying ``async_openai::Client`` (and its reqwest connection pool) is
/// built once and reused across calls, so keep-alive connections and TLS
/// sessions persist between requests — mirroring the Python integration's
/// per-model cached httpx client.
#[pyclass]
struct RustOpenAIClient {
    client: async_openai::Client<OpenAIConfig>,
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

#[pymethods]
impl RustOpenAIClient {
    #[new]
    #[pyo3(signature = (api_key=None, base_url=None, headers=None, timeout_secs=None))]
    fn new(
        api_key: Option<String>,
        base_url: Option<String>,
        headers: Option<HashMap<String, String>>,
        timeout_secs: Option<f64>,
    ) -> PyResult<Self> {
        let _ = timeout_secs; // async-openai config does not expose a per-call timeout
        let base_url = base_url.filter(|b| !b.trim().is_empty());
        let api_key = api_key.filter(|k| !k.trim().is_empty());
        let headers = headers.unwrap_or_default();
        let cfg = build_config(api_key.as_deref(), base_url.as_deref(), &headers)
            .map_err(PyRuntimeError::new_err)?;
        Ok(Self {
            client: Client::with_config(cfg),
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
            .map_err(|e| PyRuntimeError::new_err(format!("request failed: {e}")))?;
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
            .map_err(|e| PyRuntimeError::new_err(format!("request failed: {e}")))?;
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
                    .map_err(|e| format!("request failed: {e}"))?;
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
                    .map_err(|e| format!("request failed: {e}"))?;
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
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<RustOpenAIClient>()?;
    module.add_class::<StreamIter>()?;
    Ok(())
}
