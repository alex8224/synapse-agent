//! Doubao (Volcengine) streaming speech recognition, driven from the desktop shell.
//!
//! This is the Rust twin of ``synapse/stt/doubao.py``: the same binary framing, the
//! same config payload, the same ``definite`` -> finalized mapping.  It lives here
//! because the vendor authenticates the WebSocket **handshake** with headers
//! (``X-Api-Key`` and friends), and a browser's ``WebSocket`` cannot set headers --
//! so the socket is opened by this process, which also means the API key never has
//! to enter the webview.
//!
//! The wire layout is taken from the vendor's protocol section so it can be
//! reviewed rather than guessed at:
//!
//!     byte 0   protocol version (4 bits, 0b0001) | header size (4 bits, 0b0001 = 4 B)
//!     byte 1   message type (4 bits) | message-type-specific flags (4 bits)
//!     byte 2   serialization method (4 bits) | compression (4 bits)
//!     byte 3   reserved
//!     then     payload size (uint32, big-endian) + payload
//!
//! ``bigmodel_async`` with ``enable_nonstream`` streams text while the reader speaks
//! *and* re-decodes each finished sentence with the non-streaming model; only that
//! second output carries ``"definite": true``.  So ``definite`` utterances become
//! *finalized* and the remaining tail becomes the *partial* -- the same contract the
//! console already renders for the local engine.

use std::io::{Read, Write};
use std::sync::mpsc::{Receiver, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use serde::Serialize;
use serde_json::Value;
use tokio::sync::mpsc::{error::TrySendError, Sender as TokioSender};
use tokio::sync::Notify;

// --- protocol constants (mirrored from doubao.py) -----------------------------

/// The vendor's recommended mode: it returns a frame only when the result changes,
/// and it is the only mode that supports the second pass.
pub const DEFAULT_ENDPOINT: &str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async";

/// 2.0, hourly billing -- the resource the vendor's own sample defaults to.
pub const DEFAULT_RESOURCE_ID: &str = "volc.seedasr.sauc.duration";

const PROTOCOL_VERSION: u8 = 0b0001;
const HEADER_SIZE: u8 = 0b0001;
const FULL_CLIENT_REQUEST: u8 = 0b0001;
const AUDIO_ONLY_REQUEST: u8 = 0b0010;
const FULL_SERVER_RESPONSE: u8 = 0b1001;
const SERVER_ERROR: u8 = 0b1111;
const FLAG_NONE: u8 = 0b0000;
const FLAG_LAST: u8 = 0b0010;
const SERIALIZATION_JSON: u8 = 0b0001;
const COMPRESSION_NONE: u8 = 0b0000;
const COMPRESSION_GZIP: u8 = 0b0001;

/// The rate the console captures at; every packet is 16-bit PCM at this rate.
pub const SAMPLE_RATE: u32 = 16000;

/// The packet cadence the session re-frames to.
///
/// The vendor asks for 100-200 ms and warns that other sizes hurt performance.  100 ms
/// is the floor: the shell is what actually gates the vendor, so a console that sent
/// narrower chunks would gain nothing -- its extra granularity would be merged back
/// here before anything left the process.
pub const DEFAULT_PACKET_MS: u32 = 100;

/// Silence that closes a sentence.
///
/// This is the delay between a reader pausing and the sentence becoming *finalized* --
/// the vendor's default of 800 ms is conservative, and it is paid on every sentence
/// rather than once per dictation, which makes it the largest remaining lever.  Lower
/// values cut sentences at shorter pauses, and a cut sentence is inserted as its own
/// phrase, so this is a trade-off rather than a free win.
///
/// Measured against the live service: 400 ms commits a sentence 400 ms sooner (6351 ms
/// vs 6751 ms into the fixture clip) and produced the same two sentences with the same
/// boundaries, so on that clip the shorter threshold never fired inside a sentence.  A
/// longer pause-heavy clip is the thing that would settle it; if sentences start getting
/// cut at clause pauses, this is the one constant to raise.
///
/// The vendor's documented minimum is 200 ms, and 200 ms measures another 267 ms sooner
/// (6134 ms vs 6401 ms) with the fixture's boundaries still intact -- but that clip's
/// longest intra-sentence silence is 150 ms, so it *cannot* show the failure mode: a
/// 200 ms threshold is below the normal clause-pause range, and a cut sentence is
/// inserted as its own phrase.  The default therefore stops at 400.
pub const DEFAULT_END_WINDOW_MS: u32 = 400;

/// Bytes of 16-bit mono PCM in one packet of `packet_ms`.
pub fn bytes_per_packet(packet_ms: u32) -> usize {
    (SAMPLE_RATE as usize * packet_ms as usize / 1000) * 2
}

/// The two knobs the vendor exposes to us, in one place.
#[derive(Debug, Clone, Copy)]
pub struct CloudTuning {
    /// The packet cadence handed to the service.
    pub packet_ms: u32,
    /// How much silence closes a sentence.
    pub end_window_ms: u32,
    /// Whether the vendor re-decodes each finished sentence with its non-streaming
    /// model.  Only that pass marks a sentence `definite`, so turning it off commits the
    /// *streaming* text instead: sooner, and without the second pass's corrections.
    ///
    /// Measured: it costs about 270 ms per committed sentence (6068 ms vs 6337 ms into
    /// the fixture clip).  On that clip it also *hurt* -- the second pass capitalised
    /// `runtime` and `attach`, which the streaming pass had right.  One clip is not
    /// evidence that the vendor's accuracy pass is worthless, so the default keeps it;
    /// this field is the whole switch.
    pub nonstream: bool,
    /// The vendor's first-word accelerator: `None` leaves it off, `Some(score)` turns it
    /// on with a score in `0..=20`.
    ///
    /// The documentation is explicit that this trades accuracy for latency -- "尽量加速
    /// 首字返回，但会降低首字准确率" -- and that a larger score means "首字出字越快".
    /// It moves *when the first partial appears*, not what the second pass eventually
    /// commits, so it is the one knob that acts on the ~700 ms first-word floor.
    ///
    /// Measured: **no effect** on the fixture clip -- 10 and 20 both left the first
    /// partial at 700-901 ms, the same spread as leaving it off.  That is a negative
    /// result, not a refutation: the parameter table marks several neighbours as "仅
    /// nostream 接口和双向流式优化版支持", and the vendor ignores an unsupported field
    /// silently, so this may mean "not honoured on ``bigmodel_async``" rather than "no
    /// such lever".  Off by default; nothing here justifies turning it on.
    pub accelerate: Option<u8>,
}

impl Default for CloudTuning {
    fn default() -> Self {
        Self {
            packet_ms: DEFAULT_PACKET_MS,
            end_window_ms: DEFAULT_END_WINDOW_MS,
            nonstream: true,
            accelerate: None,
        }
    }
}

/// How long ``finish`` waits for the server's last frame.
const FINISH_TIMEOUT: Duration = Duration::from_secs(20);

/// A hard cap on one dictation, so a session whose owner walked away still ends.
const MAX_SESSION: Duration = Duration::from_secs(30 * 60);

/// Ceiling on one decompressed server frame (the socket's own ``max_size``).
const MAX_RESPONSE_BYTES: usize = 8 * 1024 * 1024;

/// Ceiling on one audio chunk, mirroring the service's ``max_stt_chunk_bytes``.
pub const MAX_CHUNK_BYTES: usize = 64 * 1024;

/// How many un-sent audio packets may queue before the caller is told to slow down.
/// 128 packets of 100 ms is 12.8 s of speech: long enough that only a stalled socket
/// fills it.
const AUDIO_QUEUE_PACKETS: usize = 128;

// --- updates ------------------------------------------------------------------

/// One session's progress, in the shape the console renders.
#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize)]
pub struct SttUpdate {
    /// The in-progress tail.  Caption only -- never inserted into the draft.
    pub partial: String,
    /// Sentences the vendor marked final since the previous call.
    pub finalized: Vec<String>,
}

// --- framing ------------------------------------------------------------------

fn frame_header(message_type: u8, flags: u8, gzip_payload: bool) -> [u8; 4] {
    [
        (PROTOCOL_VERSION << 4) | HEADER_SIZE,
        (message_type << 4) | flags,
        (SERIALIZATION_JSON << 4)
            | if gzip_payload {
                COMPRESSION_GZIP
            } else {
                COMPRESSION_NONE
            },
        0,
    ]
}

fn gzip_bytes(raw: &[u8]) -> Result<Vec<u8>, String> {
    let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
    encoder
        .write_all(raw)
        .map_err(|err| format!("语音请求压缩失败：{err}"))?;
    encoder
        .finish()
        .map_err(|err| format!("语音请求压缩失败：{err}"))
}

fn gunzip_bytes(raw: &[u8]) -> Result<Vec<u8>, String> {
    let mut decoder = flate2::read::GzDecoder::new(raw).take(MAX_RESPONSE_BYTES as u64 + 1);
    let mut out = Vec::new();
    decoder
        .read_to_end(&mut out)
        .map_err(|err| format!("豆包响应解压失败：{err}"))?;
    if out.len() > MAX_RESPONSE_BYTES {
        return Err("豆包响应超过大小上限".into());
    }
    Ok(out)
}

/// The first frame: the JSON config that opens the session.
pub fn build_full_client_request(payload: &Value) -> Result<Vec<u8>, String> {
    let raw = serde_json::to_vec(payload).map_err(|err| format!("语音请求序列化失败：{err}"))?;
    let body = gzip_bytes(&raw)?;
    let mut frame = Vec::with_capacity(8 + body.len());
    frame.extend_from_slice(&frame_header(FULL_CLIENT_REQUEST, FLAG_NONE, true));
    frame.extend_from_slice(&(body.len() as u32).to_be_bytes());
    frame.extend_from_slice(&body);
    Ok(frame)
}

/// One audio frame; the last one carries the flag that ends the session.
///
/// The header must agree with what was actually done to the payload -- a mismatch
/// is a protocol error, not a tolerated variant.
pub fn build_audio_request(pcm: &[u8], last: bool) -> Result<Vec<u8>, String> {
    let body = gzip_bytes(pcm)?;
    let flags = if last { FLAG_LAST } else { FLAG_NONE };
    let mut frame = Vec::with_capacity(8 + body.len());
    frame.extend_from_slice(&frame_header(AUDIO_ONLY_REQUEST, flags, true));
    frame.extend_from_slice(&(body.len() as u32).to_be_bytes());
    frame.extend_from_slice(&body);
    Ok(frame)
}

/// The config payload: model, audio format and the incremental-text switch.
pub fn request_payload(context: Option<&str>, tuning: &CloudTuning) -> Value {
    let mut request = serde_json::json!({
        "model_name": "bigmodel",
        "enable_itn": true,
        "enable_punc": true,
        // The second pass is the reason to pick this endpoint at all: the
        // non-streaming model re-decodes each sentence, and only its output
        // carries `definite`.
        "enable_nonstream": tuning.nonstream,
        "show_utterances": true,
        "end_window_size": tuning.end_window_ms,
        // Incremental text: with the default ("full") every frame repeats the whole
        // transcript, so the in-progress tail would have to be recovered by string
        // surgery -- and the second-pass sentence never matches the streaming text
        // character for character, so that surgery is unreliable.
        "result_type": "single",
    });
    if let Some(score) = tuning.accelerate {
        request["enable_accelerate_text"] = Value::Bool(true);
        request["accelerate_score"] = serde_json::json!(score.min(20));
    }
    if let Some(context) = context.filter(|value| !value.is_empty()) {
        request["corpus"] = serde_json::json!({ "context": context });
    }
    serde_json::json!({
        "user": { "uid": "synapse-console" },
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": SAMPLE_RATE,
            "bits": 16,
            "channel": 1,
            "language": "zh-CN",
        },
        "request": request,
    })
}

/// One decoded server frame.
///
/// `message_type`, `flags` and `error_code` are part of the decoded frame and are
/// asserted by the codec tests; the session itself only reads the payload and the
/// error message.
#[derive(Debug, Clone)]
pub struct DoubaoResponse {
    #[allow(dead_code)]
    pub message_type: u8,
    #[allow(dead_code)]
    pub flags: u8,
    pub payload: Option<Value>,
    #[allow(dead_code)]
    pub error_code: Option<u32>,
    pub error_message: Option<String>,
}

/// Decode one server frame.
///
/// Returns ``Err`` when the frame is shorter than its own header or payload size,
/// which means the stream is out of sync and nothing after it can be trusted.
pub fn parse_response(data: &[u8]) -> Result<DoubaoResponse, String> {
    if data.len() < 4 {
        return Err("豆包帧短于其头部".into());
    }
    let message_type = data[1] >> 4;
    let flags = data[1] & 0x0F;
    let compression = data[2] & 0x0F;
    let header_size = ((data[0] & 0x0F) as usize) * 4;
    if header_size > data.len() {
        return Err("豆包帧头长度超出帧本身".into());
    }
    let mut body = &data[header_size..];
    // A frame whose flags carry bit 0b0001 puts a sequence number between the header
    // and the payload size (observed on the live service:
    // ``11 91 10 00 | <seq:4B> | <size:4B> | {"result": ...}``).  Reading it as the
    // size is what made every response look like a corrupt payload.
    if flags & 0b0001 != 0 && body.len() >= 4 {
        body = &body[4..];
    }
    if message_type == SERVER_ERROR {
        // An error frame carries the code, then the payload size, then the message:
        // ``11 f0 10 00 | <code:4B> | <size:4B> | {"error": "..."}``.
        let code = if body.len() >= 4 {
            Some(u32::from_be_bytes([body[0], body[1], body[2], body[3]]))
        } else {
            None
        };
        let mut text = if body.len() >= 4 {
            &body[4..]
        } else {
            &body[..0]
        };
        if text.len() >= 4 {
            let size = u32::from_be_bytes([text[0], text[1], text[2], text[3]]) as usize;
            if size > 0 && size <= text.len() - 4 {
                text = &text[4..4 + size];
            }
        }
        return Ok(DoubaoResponse {
            message_type,
            flags,
            payload: None,
            error_code: code,
            error_message: Some(String::from_utf8_lossy(text).into_owned()),
        });
    }
    if body.len() < 4 {
        return Err("豆包帧未携带负载长度".into());
    }
    let size = u32::from_be_bytes([body[0], body[1], body[2], body[3]]) as usize;
    let end = (4 + size).min(body.len());
    let mut payload = body[4..end].to_vec();
    if compression == COMPRESSION_GZIP && !payload.is_empty() {
        payload = gunzip_bytes(&payload)?;
    }
    if message_type == FULL_SERVER_RESPONSE && !payload.is_empty() {
        let decoded: Value = serde_json::from_slice(&payload)
            // Never guess at a payload: a frame we cannot read means the stream is
            // not what this module thinks it is.
            .map_err(|err| format!("豆包响应不是 JSON（{err}）"))?;
        if decoded.is_object() {
            return Ok(DoubaoResponse {
                message_type,
                flags,
                payload: Some(decoded),
                error_code: None,
                error_message: None,
            });
        }
    }
    Ok(DoubaoResponse {
        message_type,
        flags,
        payload: None,
        error_code: None,
        error_message: None,
    })
}

/// The recognized text of one response, or an empty string.
pub fn response_text(payload: &Value) -> String {
    let result = payload.get("result");
    if let Some(text) = result
        .and_then(|value| value.get("text"))
        .and_then(Value::as_str)
    {
        return text.to_string();
    }
    if let Some(first) = result
        .and_then(Value::as_array)
        .and_then(|items| items.first())
    {
        if let Some(text) = first.get("text").and_then(Value::as_str) {
            return text.to_string();
        }
    }
    String::new()
}

/// The sentences this response marks as finished.
///
/// ``definite`` is the vendor's own "this sentence is final" mark and appears only
/// on the non-streaming (second-pass) output -- precisely the boundary this
/// project's two-pass design draws.
pub fn definite_sentences(payload: &Value) -> Vec<String> {
    let Some(utterances) = payload
        .get("result")
        .and_then(|value| value.get("utterances"))
        .and_then(Value::as_array)
    else {
        return Vec::new();
    };
    utterances
        .iter()
        .filter(|utterance| {
            utterance
                .get("definite")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        })
        .filter_map(|utterance| utterance.get("text").and_then(Value::as_str))
        .map(str::trim)
        .filter(|text| !text.is_empty())
        .map(str::to_string)
        .collect()
}

// --- session ------------------------------------------------------------------

/// What the socket worker reports back to the command thread.
enum WorkerEvent {
    Frame(Result<DoubaoResponse, String>),
    Done,
}

/// One dictation against Doubao.
///
/// The socket lives on its own thread with its own single-threaded runtime, and
/// this struct only moves bytes and results across -- the same shape as the Python
/// ``DoubaoSession``, so the console sees one behaviour regardless of the host.
pub struct CloudSession {
    audio: TokioSender<Option<Vec<u8>>>,
    results: Receiver<WorkerEvent>,
    cancel: Arc<Notify>,
    worker: Option<std::thread::JoinHandle<()>>,
    /// Raw 16-bit PCM not yet a full packet.
    pending: Vec<u8>,
    /// The packet cadence this session re-frames to.
    packet_bytes: usize,
    /// The current in-progress tail.
    partial: String,
    /// Sentences finalized since the last drain (the caller's return value).
    finalized: Vec<String>,
    /// Every sentence ever finalized, so a repeat is never delivered twice.
    seen: Vec<String>,
    /// The last transport or protocol failure, reported once and then cleared.
    error: Option<String>,
}

impl CloudSession {
    /// Open a connection and start pumping.
    pub fn connect(
        api_key: String,
        endpoint: &str,
        resource_id: &str,
        tuning: CloudTuning,
    ) -> Result<Self, String> {
        let (audio_tx, audio_rx) =
            tokio::sync::mpsc::channel::<Option<Vec<u8>>>(AUDIO_QUEUE_PACKETS);
        let (results_tx, results_rx) = std::sync::mpsc::channel::<WorkerEvent>();
        let cancel = Arc::new(Notify::new());

        let endpoint = endpoint.to_string();
        let resource_id = resource_id.to_string();
        // The corpus hint is not wired to a setting yet; the builder keeps it available.
        let payload = request_payload(None, &tuning);
        let worker_cancel = cancel.clone();

        let worker = std::thread::Builder::new()
            .name("doubao-stt".into())
            .spawn(move || {
                let runtime = match tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                {
                    Ok(runtime) => runtime,
                    Err(err) => {
                        let _ = results_tx.send(WorkerEvent::Frame(Err(format!(
                            "豆包连接失败：无法启动运行时（{err}）"
                        ))));
                        let _ = results_tx.send(WorkerEvent::Done);
                        return;
                    }
                };
                runtime.block_on(async move {
                    let outcome = run_socket(
                        api_key,
                        endpoint,
                        resource_id,
                        payload,
                        audio_rx,
                        results_tx.clone(),
                        worker_cancel,
                    )
                    .await;
                    if let Err(err) = outcome {
                        let _ = results_tx.send(WorkerEvent::Frame(Err(err)));
                    }
                    let _ = results_tx.send(WorkerEvent::Done);
                });
            })
            .map_err(|err| format!("豆包连接失败：无法启动工作线程（{err}）"))?;

        Ok(Self {
            audio: audio_tx,
            results: results_rx,
            cancel,
            worker: Some(worker),
            pending: Vec::new(),
            packet_bytes: bytes_per_packet(tuning.packet_ms).max(2),
            partial: String::new(),
            finalized: Vec::new(),
            seen: Vec::new(),
            error: None,
        })
    }

    /// Feed one base64 chunk of 16-bit little-endian PCM and report progress.
    ///
    /// The bytes are forwarded as they arrived: the console already encodes exactly
    /// the format the vendor wants, so a decode/re-encode round trip would only add
    /// loss and work.
    ///
    /// A malformed chunk is reported through [`Self::error`] rather than as an
    /// `Err`, because the console renders the error beside the dictation it belongs
    /// to -- the same channel a transport failure uses.
    pub fn append(&mut self, data_base64: &str) -> SttUpdate {
        let bytes = match BASE64.decode(data_base64) {
            Ok(bytes) => bytes,
            Err(_) => {
                self.error = Some("语音分片不是合法的 base64".into());
                return self.drain();
            }
        };
        if bytes.len() > MAX_CHUNK_BYTES {
            self.error = Some("语音分片超过大小上限".into());
            return self.drain();
        }
        if bytes.len() % 2 != 0 {
            self.error = Some("语音分片不是 16 位 PCM".into());
            return self.drain();
        }
        self.pending.extend_from_slice(&bytes);
        while self.pending.len() >= self.packet_bytes {
            let packet: Vec<u8> = self.pending.drain(..self.packet_bytes).collect();
            match self.audio.try_send(Some(packet)) {
                Ok(()) => {}
                Err(TrySendError::Full(_)) => {
                    self.error = Some("音频发送缓冲已满，请稍后重试".into());
                    break;
                }
                Err(TrySendError::Closed(_)) => {
                    self.error = Some("豆包连接已关闭".into());
                    break;
                }
            }
        }
        self.drain()
    }

    /// Flush the tail, close the stream, and return the last corrections.
    pub fn finish(&mut self) -> SttUpdate {
        if !self.pending.is_empty() {
            let tail = std::mem::take(&mut self.pending);
            let _ = self.audio.try_send(Some(tail));
        }
        let _ = self.audio.try_send(None);

        let deadline = Instant::now() + FINISH_TIMEOUT;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
            match self.results.recv_timeout(remaining) {
                Ok(WorkerEvent::Frame(frame)) => self.apply(frame),
                Ok(WorkerEvent::Done) | Err(_) => break,
            }
        }
        self.stop_worker();

        let mut update = self.drain();
        if !update.partial.is_empty() && update.finalized.is_empty() {
            // The service never marked the tail definite (a very short utterance, or
            // a stream that ended early): the text it did produce is still what the
            // reader said, so it is finalized rather than dropped -- unless it is a
            // sentence already handed over.  The second pass keeps reporting the sentence
            // it just committed as the live tail, so promoting that tail again would
            // insert the phrase into the draft twice.  `seen` is the same guard that
            // stops a repeated definite frame.
            let tail = std::mem::take(&mut update.partial);
            if !self.seen.contains(&tail) {
                self.seen.push(tail.clone());
                update.finalized = vec![tail];
            }
        }
        update
    }

    /// The transport or protocol failure seen so far, if any.
    pub fn error(&self) -> Option<String> {
        self.error.clone()
    }

    fn apply(&mut self, frame: Result<DoubaoResponse, String>) {
        let response = match frame {
            Ok(response) => response,
            Err(err) => {
                self.error = Some(err);
                return;
            }
        };
        if let Some(message) = response.error_message {
            self.error = Some(format!("豆包错误：{message}"));
            return;
        }
        let payload = response.payload.unwrap_or(Value::Null);
        let text = response_text(&payload);
        if !text.is_empty() {
            self.partial = text;
        }
        for sentence in definite_sentences(&payload) {
            if self.seen.contains(&sentence) {
                continue;
            }
            self.seen.push(sentence.clone());
            self.finalized.push(sentence);
        }
    }

    fn drain(&mut self) -> SttUpdate {
        while let Ok(event) = self.results.try_recv() {
            match event {
                WorkerEvent::Frame(frame) => self.apply(frame),
                WorkerEvent::Done => break,
            }
        }
        SttUpdate {
            partial: self.partial.clone(),
            finalized: std::mem::take(&mut self.finalized),
        }
    }

    /// Signal the worker and wait for it, so no socket outlives its session.
    fn stop_worker(&mut self) {
        self.cancel.notify_one();
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }

    #[cfg(test)]
    fn drain_blocking(&mut self, budget: Duration) -> SttUpdate {
        let deadline = Instant::now() + budget;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                break;
            }
            match self.results.recv_timeout(remaining) {
                Ok(WorkerEvent::Frame(frame)) => self.apply(frame),
                Ok(WorkerEvent::Done) | Err(_) => break,
            }
        }
        self.drain()
    }
}

impl Drop for CloudSession {
    fn drop(&mut self) {
        self.stop_worker();
    }
}

/// The socket half: connect, pump audio out, pump results in, then close.
///
/// Insert one handshake header, refusing a value that cannot be one.
///
/// The error never quotes the value: for `X-Api-Key` that value is the credential.
fn set_header(
    headers: &mut tokio_tungstenite::tungstenite::http::HeaderMap,
    name: &'static str,
    value: &str,
) -> Result<(), String> {
    let parsed = tokio_tungstenite::tungstenite::http::HeaderValue::from_str(value)
        .map_err(|_| format!("豆包连接失败：{name} 不是合法的请求头"))?;
    headers.insert(name, parsed);
    Ok(())
}

async fn run_socket(
    api_key: String,
    endpoint: String,
    resource_id: String,
    payload: Value,
    mut audio: tokio::sync::mpsc::Receiver<Option<Vec<u8>>>,
    results: Sender<WorkerEvent>,
    cancel: Arc<Notify>,
) -> Result<(), String> {
    use futures_util::{SinkExt, StreamExt};
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;
    use tokio_tungstenite::tungstenite::Message;

    let mut request = endpoint
        .as_str()
        .into_client_request()
        .map_err(|err| format!("豆包连接失败：地址无效（{err}）"))?;
    {
        let headers = request.headers_mut();
        set_header(headers, "X-Api-Key", &api_key)?;
        set_header(headers, "X-Api-Resource-Id", &resource_id)?;
        set_header(
            headers,
            "X-Api-Request-Id",
            &uuid::Uuid::new_v4().to_string(),
        )?;
        set_header(headers, "X-Api-Sequence", "-1")?;
    }

    let (socket, _response) = tokio_tungstenite::connect_async(request)
        .await
        .map_err(|err| format!("豆包连接失败：{err}"))?;
    let (mut sink, mut stream) = socket.split();

    let opening = build_full_client_request(&payload)?;
    sink.send(Message::binary(opening))
        .await
        .map_err(|err| format!("豆包连接失败：{err}"))?;

    let send_task = tokio::spawn(async move {
        while let Some(packet) = audio.recv().await {
            let frame = match &packet {
                Some(pcm) => build_audio_request(pcm, false),
                None => build_audio_request(&[], true),
            };
            let frame = match frame {
                Ok(frame) => frame,
                Err(err) => return Err(err),
            };
            if let Err(err) = sink.send(Message::binary(frame)).await {
                return Err(format!("豆包连接中断：{err}"));
            }
            if packet.is_none() {
                break;
            }
        }
        let _ = sink.close().await;
        Ok::<(), String>(())
    });

    let deadline = tokio::time::sleep(MAX_SESSION);
    tokio::pin!(deadline);
    let mut failure: Option<String> = None;
    loop {
        tokio::select! {
            _ = cancel.notified() => break,
            _ = &mut deadline => {
                failure = Some("豆包会话超过最长时长，已结束".into());
                break;
            }
            message = stream.next() => {
                match message {
                    Some(Ok(Message::Binary(data))) => {
                        let _ = results.send(WorkerEvent::Frame(parse_response(&data)));
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    Some(Ok(_)) => {}
                    Some(Err(err)) => {
                        failure = Some(format!("豆包连接中断：{err}"));
                        break;
                    }
                }
            }
        }
    }

    // The receive loop is what owns the session's lifetime, so the sender goes with
    // it: an un-sent packet must not keep the socket alive after this returns.
    send_task.abort();
    match failure {
        Some(err) => Err(err),
        None => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gunzip(frame: &[u8]) -> Vec<u8> {
        let size = u32::from_be_bytes([frame[4], frame[5], frame[6], frame[7]]) as usize;
        assert_eq!(
            size,
            frame.len() - 8,
            "the size field must describe the payload"
        );
        gunzip_bytes(&frame[8..]).expect("the payload is gzip")
    }

    /// A session with no socket: the codec-facing half, driven directly.
    fn bare_session() -> CloudSession {
        queued_session(AUDIO_QUEUE_PACKETS).0
    }

    /// One packet at the default cadence.
    fn packet_bytes() -> usize {
        bytes_per_packet(DEFAULT_PACKET_MS)
    }

    /// A session plus the receiving end of its audio queue.
    fn queued_session(
        capacity: usize,
    ) -> (CloudSession, tokio::sync::mpsc::Receiver<Option<Vec<u8>>>) {
        let (audio_tx, audio_rx) = tokio::sync::mpsc::channel(capacity);
        let session = CloudSession {
            audio: audio_tx,
            // The sender is dropped, so the receiver reports `Disconnected` and every
            // drain ends immediately: there is no worker in these tests.
            results: std::sync::mpsc::channel().1,
            cancel: Arc::new(Notify::new()),
            worker: None,
            pending: Vec::new(),
            packet_bytes: packet_bytes(),
            partial: String::new(),
            finalized: Vec::new(),
            seen: Vec::new(),
            error: None,
        };
        (session, audio_rx)
    }

    #[test]
    fn a_config_frame_carries_the_json_config_as_gzip() {
        let payload = request_payload(None, &CloudTuning::default());
        let frame = build_full_client_request(&payload).expect("frame");
        assert_eq!(frame[0], 0b0001_0001);
        assert_eq!(frame[1], (FULL_CLIENT_REQUEST << 4) | FLAG_NONE);
        assert_eq!(frame[2], (SERIALIZATION_JSON << 4) | COMPRESSION_GZIP);
        assert_eq!(frame[3], 0);
        let decoded: Value = serde_json::from_slice(&gunzip(&frame)).expect("json");
        assert_eq!(decoded, payload);
    }

    #[test]
    fn the_config_asks_for_incremental_text_and_the_second_pass() {
        let payload = request_payload(None, &CloudTuning::default());
        assert_eq!(payload["request"]["result_type"], "single");
        assert_eq!(payload["request"]["enable_nonstream"], true);
        assert_eq!(payload["request"]["end_window_size"], DEFAULT_END_WINDOW_MS);
        assert_eq!(payload["audio"]["rate"], SAMPLE_RATE);
        assert_eq!(payload["audio"]["bits"], 16);
        assert_eq!(payload["audio"]["channel"], 1);
        assert_eq!(payload["user"]["uid"], "synapse-console");
        assert!(payload["request"].get("corpus").is_none());
    }

    #[test]
    fn context_is_sent_as_a_corpus_hint_when_present() {
        let payload = request_payload(Some("Synapse 控制台"), &CloudTuning::default());
        assert_eq!(payload["request"]["corpus"]["context"], "Synapse 控制台");
        let empty = request_payload(Some(""), &CloudTuning::default());
        assert!(empty["request"].get("corpus").is_none());
    }

    #[test]
    fn the_end_window_is_the_callers_to_choose() {
        // It is the delay between a reader pausing and the sentence becoming final, so
        // it has to travel as a value rather than as a constant baked into the builder.
        let payload = request_payload(
            None,
            &CloudTuning {
                end_window_ms: 200,
                ..CloudTuning::default()
            },
        );
        assert_eq!(payload["request"]["end_window_size"], 200);
    }

    #[test]
    fn the_second_pass_is_the_callers_to_choose() {
        // It decides whether a committed sentence is the corrected one or the streaming
        // one, which is a latency-versus-quality trade rather than a fixed detail.
        let off = CloudTuning {
            nonstream: false,
            ..CloudTuning::default()
        };
        assert_eq!(
            request_payload(None, &CloudTuning::default())["request"]["enable_nonstream"],
            true
        );
        assert_eq!(
            request_payload(None, &off)["request"]["enable_nonstream"],
            false
        );
    }

    #[test]
    fn the_first_word_accelerator_is_sent_only_when_it_is_on() {
        // It is the vendor's documented latency-for-accuracy trade, so an off switch must
        // mean the fields are absent rather than sent as false/0.
        let off = request_payload(None, &CloudTuning::default());
        assert!(off["request"].get("enable_accelerate_text").is_none());
        assert!(off["request"].get("accelerate_score").is_none());

        let on = request_payload(
            None,
            &CloudTuning {
                accelerate: Some(20),
                ..CloudTuning::default()
            },
        );
        assert_eq!(on["request"]["enable_accelerate_text"], true);
        assert_eq!(on["request"]["accelerate_score"], 20);

        // The documented range is 0..=20; a larger score must not reach the wire.
        let clamped = request_payload(
            None,
            &CloudTuning {
                accelerate: Some(99),
                ..CloudTuning::default()
            },
        );
        assert_eq!(clamped["request"]["accelerate_score"], 20);
    }

    #[test]
    fn an_audio_frame_round_trips_its_pcm() {
        let pcm = vec![0x01u8, 0x02, 0x03, 0x04];
        let frame = build_audio_request(&pcm, false).expect("frame");
        assert_eq!(frame[1], (AUDIO_ONLY_REQUEST << 4) | FLAG_NONE);
        assert_eq!(gunzip(&frame), pcm);
    }

    #[test]
    fn the_last_audio_frame_sets_the_flag_and_may_be_empty() {
        let frame = build_audio_request(&[], true).expect("frame");
        assert_eq!(frame[1], (AUDIO_ONLY_REQUEST << 4) | FLAG_LAST);
        assert_eq!(gunzip(&frame), Vec::<u8>::new());
    }

    /// Build a synthetic server frame the way the live service lays one out.
    fn server_frame(message_type: u8, flags: u8, body: &[u8], gzip_body: bool) -> Vec<u8> {
        let payload = if gzip_body {
            gzip_bytes(body).unwrap()
        } else {
            body.to_vec()
        };
        let mut frame = frame_header(message_type, flags, gzip_body).to_vec();
        frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        frame.extend_from_slice(&payload);
        frame
    }

    #[test]
    fn a_gzipped_json_response_is_decoded() {
        let body = r#"{"result": {"text": "你好"}}"#.as_bytes();
        let frame = server_frame(FULL_SERVER_RESPONSE, FLAG_NONE, body, true);
        let response = parse_response(&frame).expect("frame");
        assert_eq!(response.message_type, FULL_SERVER_RESPONSE);
        let payload = response.payload.expect("a decoded payload");
        assert_eq!(response_text(&payload), "你好");
    }

    #[test]
    fn a_sequenced_frame_skips_its_sequence_number() {
        // ``11 91 10 00 | <seq:4B> | <size:4B> | payload``, as observed live.
        let body = r#"{"result": {"text": "嗯"}}"#.as_bytes();
        let payload = gzip_bytes(body).unwrap();
        let mut frame = vec![
            0b0001_0001,
            (FULL_SERVER_RESPONSE << 4) | 0b0001,
            0b0001_0001,
            0,
        ];
        frame.extend_from_slice(&7u32.to_be_bytes());
        frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        frame.extend_from_slice(&payload);
        let response = parse_response(&frame).expect("frame");
        assert_eq!(response_text(&response.payload.expect("payload")), "嗯");
    }

    #[test]
    fn an_error_frame_reports_the_vendor_message() {
        // ``11 f0 10 00 | <code:4B> | <size:4B> | {"error": "..."}``.
        let message = br#"{"error": "invalid request"}"#;
        // An error frame carries its code where the other frames carry their payload
        // size, so it is laid out directly rather than through `server_frame`.
        let mut frame = frame_header(SERVER_ERROR, FLAG_NONE, false).to_vec();
        frame.extend_from_slice(&40_000_123u32.to_be_bytes());
        frame.extend_from_slice(&(message.len() as u32).to_be_bytes());
        frame.extend_from_slice(message);
        let response = parse_response(&frame).expect("frame");
        assert_eq!(response.error_code, Some(40_000_123));
        assert!(response
            .error_message
            .unwrap_or_default()
            .contains("invalid request"));
    }

    #[test]
    fn a_truncated_frame_is_refused() {
        assert!(parse_response(&[0x11, 0x91]).is_err());
        // A header that promises more bytes than the frame holds.
        assert!(parse_response(&[0b0001_0010, 0x91, 0x10, 0x00, 0x00]).is_err());
    }

    #[test]
    fn a_header_size_beyond_the_frame_is_refused() {
        // header size nibble = 0b1111 -> 60 bytes, but the frame is 4.
        assert!(parse_response(&[0b0001_1111, 0x91, 0x10, 0x00]).is_err());
    }

    #[test]
    fn definite_utterances_are_finalized_and_the_rest_is_the_partial() {
        let payload: Value = serde_json::json!({
            "result": {
                "text": "重复提交",
                "utterances": [
                    {"definite": true, "text": " 重复提交 "},
                    {"definite": false, "text": "attach"},
                ],
            }
        });
        assert_eq!(response_text(&payload), "重复提交");
        assert_eq!(definite_sentences(&payload), vec!["重复提交".to_string()]);
    }

    #[test]
    fn a_response_without_utterances_finalizes_nothing() {
        let payload: Value = serde_json::json!({"result": {"text": "在"}});
        assert!(definite_sentences(&payload).is_empty());
        assert!(definite_sentences(&Value::Null).is_empty());
        assert_eq!(response_text(&Value::Null), "");
    }

    #[test]
    fn a_result_array_is_read_like_a_result_object() {
        let payload: Value = serde_json::json!({"result": [{"text": "第一句"}]});
        assert_eq!(response_text(&payload), "第一句");
    }

    #[test]
    fn the_update_deduplicates_a_repeated_sentence() {
        let mut session = bare_session();
        let frame = || {
            Ok(DoubaoResponse {
                message_type: FULL_SERVER_RESPONSE,
                flags: FLAG_NONE,
                payload: Some(serde_json::json!({
                    "result": {"text": "好", "utterances": [{"definite": true, "text": "好"}]}
                })),
                error_code: None,
                error_message: None,
            })
        };
        session.apply(frame());
        assert_eq!(session.drain().finalized, vec!["好".to_string()]);
        session.apply(frame());
        assert!(
            session.drain().finalized.is_empty(),
            "a repeat is not re-delivered"
        );
    }

    #[test]
    fn a_broken_frame_surfaces_as_the_session_error() {
        let mut session = bare_session();
        session.apply(Err("豆包帧短于其头部".into()));
        assert_eq!(session.error().as_deref(), Some("豆包帧短于其头部"));
    }

    #[test]
    fn the_tail_is_not_committed_twice_when_the_vendor_already_said_it() {
        // The second pass reports the sentence it just committed as the live tail as
        // well.  A live run caught `finish` promoting that tail into a *second*
        // finalized sentence, which the console would have inserted into the draft
        // twice -- so the promotion has to respect the same "already delivered" guard as
        // a repeated definite frame.
        let mut session = bare_session();
        session.apply(Ok(DoubaoResponse {
            message_type: FULL_SERVER_RESPONSE,
            flags: FLAG_NONE,
            payload: Some(serde_json::json!({
                "result": {"text": "好", "utterances": [{"definite": true, "text": "好"}]}
            })),
            error_code: None,
            error_message: None,
        }));
        assert_eq!(session.drain().finalized, vec!["好".to_string()]);
        let update = session.finish();
        assert!(update.finalized.is_empty(), "{:?}", update.finalized);
    }

    #[test]
    fn append_reports_an_oversized_or_odd_chunk_as_the_session_error() {
        let mut session = bare_session();
        session.append("not base64!!");
        assert_eq!(
            session.error().as_deref(),
            Some("语音分片不是合法的 base64")
        );
        session.append(&BASE64.encode([1u8, 2, 3]));
        assert_eq!(session.error().as_deref(), Some("语音分片不是 16 位 PCM"));
        let huge = BASE64.encode(vec![0u8; MAX_CHUNK_BYTES + 2]);
        session.append(&huge);
        assert_eq!(session.error().as_deref(), Some("语音分片超过大小上限"));
    }

    #[test]
    fn append_only_emits_whole_200ms_packets() {
        let (mut session, mut audio_rx) = queued_session(AUDIO_QUEUE_PACKETS);
        // One packet minus two bytes: nothing may be sent yet.
        let short = vec![0u8; packet_bytes() - 2];
        session.append(&BASE64.encode(&short));
        assert!(
            audio_rx.try_recv().is_err(),
            "a partial packet must not be sent"
        );

        // Two more bytes complete exactly one packet.
        session.append(&BASE64.encode([0u8, 0u8]));
        let packet = audio_rx.try_recv().expect("one packet");
        assert_eq!(packet.as_deref().map(<[u8]>::len), Some(packet_bytes()));
        assert!(
            audio_rx.try_recv().is_err(),
            "only one packet was completed"
        );
    }

    #[test]
    fn a_full_audio_queue_is_reported_rather_than_growing_without_bound() {
        let (mut session, _audio_rx) = queued_session(1);
        let chunk = BASE64.encode(vec![0u8; packet_bytes() * 2]);
        session.append(&chunk);
        assert!(
            session.error().is_some(),
            "a stalled socket must be visible"
        );
    }

    /// The one thing a fake cannot vouch for is the handshake, so the session is
    /// driven against a real socket here: the four headers must arrive, audio must
    /// leave in whole 200 ms packets, and the stream must end with a last packet.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn a_session_sends_the_required_headers_and_streams_text_back() {
        use futures_util::{SinkExt, StreamExt};
        use tokio_tungstenite::tungstenite::Message;

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind a loopback port");
        let port = listener.local_addr().expect("local addr").port();
        let seen_headers: Arc<std::sync::Mutex<Vec<(String, String)>>> =
            Arc::new(std::sync::Mutex::new(Vec::new()));
        let server_headers = seen_headers.clone();

        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("accept");
            let mut socket = tokio_tungstenite::accept_hdr_async(
                stream,
                move |request: &tokio_tungstenite::tungstenite::handshake::server::Request,
                      response: tokio_tungstenite::tungstenite::handshake::server::Response| {
                    let mut collected = server_headers.lock().expect("lock");
                    for (name, value) in request.headers() {
                        collected.push((
                            name.as_str().to_ascii_lowercase(),
                            value.to_str().unwrap_or_default().to_string(),
                        ));
                    }
                    Ok(response)
                },
            )
            .await
            .expect("handshake");

            // The opening frame is the JSON config.
            let opening = socket.next().await.expect("a frame").expect("a frame");
            let Message::Binary(opening) = opening else {
                panic!("the first frame must be binary");
            };
            assert_eq!(opening[1] >> 4, FULL_CLIENT_REQUEST);

            // Then one result carrying a sentence the vendor marked definite.
            let body = r#"{"result": {"text": "你好", "utterances": [{"definite": true, "text": "你好"}]}}"#;
            socket
                .send(Message::binary(server_frame(
                    FULL_SERVER_RESPONSE,
                    FLAG_NONE,
                    body.as_bytes(),
                    true,
                )))
                .await
                .expect("send a result");

            // Audio must arrive in whole packets and end with the last flag.
            let mut packets = 0usize;
            let mut saw_last = false;
            while let Some(message) = socket.next().await {
                let Ok(Message::Binary(data)) = message else {
                    break;
                };
                assert_eq!(data[1] >> 4, AUDIO_ONLY_REQUEST);
                let payload_size =
                    u32::from_be_bytes([data[4], data[5], data[6], data[7]]) as usize;
                if data[1] & 0x0F == FLAG_LAST {
                    assert_eq!(payload_size, gzip_bytes(&[]).expect("gzip").len());
                    saw_last = true;
                    break;
                }
                assert_eq!(gunzip(&data), vec![0u8; packet_bytes()], "a whole packet");
                packets += 1;
            }
            assert_eq!(packets, 2, "two packets were appended");
            assert!(saw_last, "the stream must end with a last packet");
        });

        let mut session = CloudSession::connect(
            "test-key".to_string(),
            &format!("ws://127.0.0.1:{port}"),
            DEFAULT_RESOURCE_ID,
            CloudTuning::default(),
        )
        .expect("connect");

        session.append(&BASE64.encode(vec![0u8; packet_bytes() * 2]));
        let update = session.drain_blocking(Duration::from_secs(5));
        assert_eq!(update.partial, "你好");
        assert_eq!(update.finalized, vec!["你好".to_string()]);

        // `finish` must return on the server's close rather than waiting out the
        // 20 s budget, and it must take the worker thread with it.
        let started = Instant::now();
        session.finish();
        assert!(
            started.elapsed() < FINISH_TIMEOUT,
            "finish must not wait out its whole budget"
        );
        assert!(session.worker.is_none(), "the socket thread must be gone");

        server.await.expect("the mock server finishes");
        let headers = seen_headers.lock().expect("lock");
        let find = |name: &str| {
            headers
                .iter()
                .find(|(key, _)| key == name)
                .map(|(_, value)| value.clone())
        };
        assert_eq!(find("x-api-key").as_deref(), Some("test-key"));
        assert_eq!(
            find("x-api-resource-id").as_deref(),
            Some(DEFAULT_RESOURCE_ID)
        );
        assert_eq!(find("x-api-sequence").as_deref(), Some("-1"));
        let request_id = find("x-api-request-id").expect("a request id");
        assert_eq!(request_id.len(), 36, "a uuid: {request_id}");
    }

    /// A real handshake against the vendor, to prove the TLS stack and the header set
    /// are right.  Ignored by default: it needs the network and a live service, and a
    /// fake key is enough -- the vendor's own rejection is the signal.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    #[ignore = "needs network access to the vendor endpoint"]
    async fn the_live_endpoint_rejects_a_fake_key_instead_of_failing_to_connect() {
        let mut session = CloudSession::connect(
            "probe-not-a-real-key".to_string(),
            DEFAULT_ENDPOINT,
            DEFAULT_RESOURCE_ID,
            CloudTuning::default(),
        )
        .expect("the worker starts");
        // The socket is opened on the worker thread, so a handshake failure arrives as
        // a session error rather than as a connect error.
        let update = session.drain_blocking(Duration::from_secs(20));
        assert!(update.finalized.is_empty());
        let error = session.error().unwrap_or_default();
        assert!(
            error.contains("Invalid X-Api-Key") || error.contains("401"),
            "expected the vendor's own rejection, got: {error}"
        );
    }

    /// The 16 kHz mono int16 PCM inside a canonical RIFF/WAVE file.
    fn read_wav_pcm(path: &std::path::Path) -> Vec<u8> {
        let bytes = std::fs::read(path).unwrap_or_else(|err| panic!("{}: {err}", path.display()));
        assert_eq!(&bytes[0..4], b"RIFF", "not a RIFF file");
        assert_eq!(&bytes[8..12], b"WAVE", "not a WAVE file");
        let mut offset = 12usize;
        while offset + 8 <= bytes.len() {
            let id = &bytes[offset..offset + 4];
            let size = u32::from_le_bytes([
                bytes[offset + 4],
                bytes[offset + 5],
                bytes[offset + 6],
                bytes[offset + 7],
            ]) as usize;
            let body = offset + 8;
            if id == b"fmt ".as_slice() {
                let channels = u16::from_le_bytes([bytes[body + 2], bytes[body + 3]]);
                let rate = u32::from_le_bytes([
                    bytes[body + 4],
                    bytes[body + 5],
                    bytes[body + 6],
                    bytes[body + 7],
                ]);
                let bits = u16::from_le_bytes([bytes[body + 14], bytes[body + 15]]);
                assert_eq!(
                    (channels, rate, bits),
                    (1u16, SAMPLE_RATE, 16u16),
                    "the fixture must be 16 kHz mono int16 PCM"
                );
            }
            if id == b"data".as_slice() {
                return bytes[body..body + size].to_vec();
            }
            offset = body + size + (size % 2);
        }
        panic!("the fixture carries no data chunk");
    }

    /// The end-to-end smoke: the real key, the real service, real speech.
    ///
    /// Ignored by default -- it needs the network, a stored key, and it spends money on
    /// a **billable** vendor call.  It is the only check that can prove the handshake,
    /// the packet cadence and the `definite` mapping against the live service rather
    /// than against a fake, and it needs no microphone: it replays the same fixture the
    /// offline engine's smoke test uses.
    ///
    /// ```text
    /// cargo test --manifest-path rust/synapse-gui/Cargo.toml -- --ignored --nocapture
    /// ```
    #[test]
    #[ignore = "needs the network, a stored Doubao key, and a billable vendor call"]
    fn the_live_service_transcribes_the_fixture_clip() {
        let provider = crate::stt::credentials::DOUBAO_PROVIDER_ID;
        let Some(api_key) = crate::stt::credentials::api_key(provider) else {
            eprintln!("no {provider} key stored; nothing to smoke");
            return;
        };
        let clip = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/stt/speech.wav");
        let pcm = read_wav_pcm(&clip);
        eprintln!(
            "clip: {} bytes, {:.1} s",
            pcm.len(),
            pcm.len() as f32 / 2.0 / SAMPLE_RATE as f32
        );

        let tuning = CloudTuning::default();
        let mut session =
            CloudSession::connect(api_key, DEFAULT_ENDPOINT, DEFAULT_RESOURCE_ID, tuning)
                .expect("the worker starts");

        let mut partials: Vec<String> = Vec::new();
        let mut finalized: Vec<String> = Vec::new();
        let started = Instant::now();
        for (index, packet) in pcm.chunks(bytes_per_packet(tuning.packet_ms)).enumerate() {
            let update = session.append(&BASE64.encode(packet));
            if !update.partial.is_empty() && partials.last() != Some(&update.partial) {
                partials.push(update.partial);
            }
            finalized.extend(update.finalized);
            // Pace at the packet's own duration, so the service sees the clip the way it
            // would see a microphone instead of one instant burst -- and so the bounded
            // audio queue is never the thing under test.
            let due = Duration::from_millis(tuning.packet_ms as u64 * (index as u64 + 1));
            if let Some(wait) = due.checked_sub(started.elapsed()) {
                std::thread::sleep(wait);
            }
        }
        finalized.extend(session.finish().finalized);
        if let Some(error) = session.error() {
            panic!("the live service reported: {error}");
        }

        let transcript = finalized.join("");
        eprintln!("partials: {partials:?}");
        eprintln!("finalized: {finalized:?}");
        assert!(
            !partials.is_empty(),
            "the live pass must produce live text; that is what the caption shows"
        );
        assert!(
            !finalized.is_empty(),
            "the second pass must close at least one sentence"
        );
        assert!(transcript.contains("重复提交"), "{transcript}");
        // The English terms are why this engine is worth having: a recognizer that is
        // only good at Chinese renders them as syllables.
        assert!(transcript.to_lowercase().contains("attach"), "{transcript}");
    }

    /// When the text first showed, for one configuration.
    struct Measured {
        first_text: Duration,
        marker: Duration,
        first_finalized: Duration,
        updates: usize,
        /// A transport or protocol failure, if the vendor reported one.
        error: Option<String>,
        /// The sentences the run committed, in order.
        finalized: Vec<String>,
    }

    /// Replay the clip at real time through one configuration and time the text.
    ///
    /// The console's window is a *hard* additive delay: audio cannot leave until the
    /// window fills, so chunk `index` is sent at the end of its own window.  The clip is
    /// paced so the audio timeline *is* the wall clock, which makes these timings
    /// directly comparable to "how long after the reader said it did the word appear".
    ///
    /// `chunk_bytes` is what the console sends; the session re-frames it to
    /// `tuning.packet_ms`, so a chunk narrower than the tuning is merged back and buys
    /// nothing -- which is the whole reason both knobs are parameters.
    fn measure_window(
        api_key: &str,
        pcm: &[u8],
        chunk_bytes: usize,
        tuning: CloudTuning,
        marker: &str,
    ) -> Measured {
        let mut session = CloudSession::connect(
            api_key.to_string(),
            DEFAULT_ENDPOINT,
            DEFAULT_RESOURCE_ID,
            tuning,
        )
        .expect("the worker starts");
        let window_ms = (chunk_bytes / 2) as u64 * 1000 / SAMPLE_RATE as u64;
        let started = Instant::now();
        let mut first_text: Option<Duration> = None;
        let mut marker_at: Option<Duration> = None;
        let mut first_finalized: Option<Duration> = None;
        let mut updates = 0usize;
        let mut last = String::new();
        let mut finalized: Vec<String> = Vec::new();

        for (index, packet) in pcm.chunks(chunk_bytes).enumerate() {
            // Wait for the window to fill before handing it over.
            let due = Duration::from_millis(window_ms * (index as u64 + 1));
            if let Some(wait) = due.checked_sub(started.elapsed()) {
                std::thread::sleep(wait);
            }
            let update = session.append(&BASE64.encode(packet));
            let elapsed = started.elapsed();
            if !update.partial.is_empty() && update.partial != last {
                updates += 1;
                last = update.partial.clone();
            }
            if first_text.is_none() && !update.partial.is_empty() {
                first_text = Some(elapsed);
            }
            if marker_at.is_none() && update.partial.contains(marker) {
                marker_at = Some(elapsed);
            }
            // A sentence only becomes final once the vendor has heard `end_window_ms` of
            // silence, so this is the delay between a pause and the text being committed.
            if first_finalized.is_none() && !update.finalized.is_empty() {
                first_finalized = Some(elapsed);
            }
            finalized.extend(update.finalized);
        }
        finalized.extend(session.finish().finalized);
        Measured {
            first_text: first_text.unwrap_or(Duration::ZERO),
            marker: marker_at.unwrap_or(Duration::ZERO),
            first_finalized: first_finalized.unwrap_or(Duration::ZERO),
            updates,
            error: session.error(),
            finalized,
        }
    }

    /// The measurement behind the two tuning knobs.
    ///
    /// Same clip, same service, same key: only the configuration differs.  It exists
    /// because "a narrower window is faster" is worth checking rather than assuming, and
    /// because the two knobs act on different things -- the packet cadence on when a
    /// word first appears, the end window on how long after a pause a sentence is
    /// committed.
    ///
    /// Ignored by default: it needs the network, a stored key, and it is billed -- twelve
    /// calls, about three minutes.  The sample size is not decoration: two trials per
    /// window once came out *inconclusive* (one of them had the 600 ms window ahead),
    /// because the vendor decides for itself when it has heard enough to emit its first
    /// partial and that swing is wider than the window.
    #[test]
    #[ignore = "needs the network, a stored Doubao key, and billable vendor calls"]
    fn the_narrower_window_shows_text_sooner() {
        let provider = crate::stt::credentials::DOUBAO_PROVIDER_ID;
        let Some(api_key) = crate::stt::credentials::api_key(provider) else {
            eprintln!("no {provider} key stored; nothing to measure");
            return;
        };
        let clip = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/stt/speech.wav");
        let pcm = read_wav_pcm(&clip);
        const MARKER: &str = "帮我检查";
        let configs = [
            (
                "current default (100/400, 2nd pass)",
                CloudTuning {
                    packet_ms: 100,
                    end_window_ms: 400,
                    ..CloudTuning::default()
                },
            ),
            (
                "end 200",
                CloudTuning {
                    end_window_ms: 200,
                    ..CloudTuning::default()
                },
            ),
            (
                "end 200, no 2nd pass",
                CloudTuning {
                    end_window_ms: 200,
                    nonstream: false,
                    ..CloudTuning::default()
                },
            ),
        ];
        // Interleaved trials: the vendor decides for itself when it has heard enough to
        // emit its first partial (observed at 600 ms and at 1200 ms of audio), and that
        // swing is wider than the window itself -- so one sample proves nothing.
        // `SYNAPSE_STT_TRIALS=1` is enough when only the committed sentences are of
        // interest, which is the cheap way to judge the end window on quality.
        let trials = std::env::var("SYNAPSE_STT_TRIALS")
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(4);
        for trial in 0..trials {
            for (label, tuning) in configs {
                let measured = measure_window(
                    &api_key,
                    &pcm,
                    bytes_per_packet(tuning.packet_ms),
                    tuning,
                    MARKER,
                );
                eprintln!(
                    "trial {trial} {label}: first text {:>6.0} ms, \"{MARKER}\" {:>6.0} ms, \
                     first finalized {:>6.0} ms, {} updates, error {:?}",
                    measured.first_text.as_secs_f64() * 1000.0,
                    measured.marker.as_secs_f64() * 1000.0,
                    measured.first_finalized.as_secs_f64() * 1000.0,
                    measured.updates,
                    measured.error,
                );
                eprintln!("           committed: {:?}", measured.finalized);
            }
        }
    }

    /// Does the narrow end window ever leave the vendor silent?
    ///
    /// One trial of the comparison above returned nothing for 6.4 s and then delivered
    /// everything at once -- two updates where the others made twenty-odd.  That is
    /// either a stalled socket or a real consequence of asking the vendor to close
    /// sentences sooner, and only the second would argue against the setting, so it gets
    /// its own repeatable run instead of being written off as noise.
    ///
    /// Ignored by default: it needs the network, a stored key, and it is billed.
    #[test]
    #[ignore = "needs the network, a stored Doubao key, and billable vendor calls"]
    fn the_narrow_end_window_is_repeatable() {
        let provider = crate::stt::credentials::DOUBAO_PROVIDER_ID;
        let Some(api_key) = crate::stt::credentials::api_key(provider) else {
            eprintln!("no {provider} key stored; nothing to measure");
            return;
        };
        let clip = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/stt/speech.wav");
        let pcm = read_wav_pcm(&clip);
        let tuning = CloudTuning {
            packet_ms: 100,
            end_window_ms: 400,
            nonstream: true,
            accelerate: None,
        };
        for trial in 0..6 {
            let measured = measure_window(
                &api_key,
                &pcm,
                bytes_per_packet(tuning.packet_ms),
                tuning,
                "帮我检查",
            );
            eprintln!(
                "trial {trial} 400 ms end: first text {:>6.0} ms, first finalized {:>6.0} ms, \
                 {} updates, error {:?}",
                measured.first_text.as_secs_f64() * 1000.0,
                measured.first_finalized.as_secs_f64() * 1000.0,
                measured.updates,
                measured.error,
            );
        }
    }
}
