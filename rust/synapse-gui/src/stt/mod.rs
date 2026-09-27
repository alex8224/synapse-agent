//! Cloud speech recognition, owned by the desktop shell rather than the daemon.
//!
//! The vendor authenticates the WebSocket handshake with headers, and a browser's
//! ``WebSocket`` cannot set headers -- so the desktop shell opens the socket itself
//! and the API key stays in this process.  The daemon keeps serving the offline ONNX
//! engine; this module only ever handles the cloud one.

pub mod credentials;
pub mod doubao;

use std::collections::HashMap;

use serde::Serialize;

pub use doubao::CloudSession;

/// The provider id the cloud engine is registered under (``stt.providers``).
pub const CLOUD_PROVIDER_ID: &str = credentials::DOUBAO_PROVIDER_ID;

/// Whether the cloud engine can run, and why not when it cannot.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SttCloudStatus {
    pub available: bool,
    /// Whether a key is stored.  The key itself never crosses this boundary.
    pub key_configured: bool,
    pub reason: Option<String>,
    pub sample_rate: u32,
}

/// A freshly opened dictation.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SttCloudBegin {
    pub session_id: u64,
    pub sample_rate: u32,
}

/// One chunk's worth of progress, shaped like the daemon's ``SttAppendResult`` so
/// the console can swap transports without changing how it renders a dictation.
#[derive(Debug, Default, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SttCloudAppend {
    pub partial: String,
    pub finalized: Vec<String>,
    pub error: Option<String>,
}

/// The sentences produced by closing a dictation.
#[derive(Debug, Default, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SttCloudFinish {
    pub finalized: Vec<String>,
}

/// Whether a dictation was open and has now been torn down.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SttCloudCancel {
    pub cancelled: bool,
}

/// Every cloud dictation this process has open.
///
/// The console dictates one at a time, so the map holds at most one session; it is
/// keyed by an id anyway because that is what lets a stale `append` from a previous
/// dictation be ignored instead of landing in the new one.
#[derive(Default)]
pub struct SttManager {
    next_id: u64,
    sessions: HashMap<u64, CloudSession>,
}

impl SttManager {
    pub fn new() -> Self {
        Self {
            next_id: 0,
            sessions: HashMap::new(),
        }
    }

    /// Whether the cloud engine is usable, in the console's terms.
    pub fn status() -> SttCloudStatus {
        let key_configured = credentials::configured(CLOUD_PROVIDER_ID);
        let reason = if key_configured {
            None
        } else {
            credentials::config_error().or_else(|| Some("尚未配置豆包 API Key".into()))
        };
        SttCloudStatus {
            available: key_configured,
            key_configured,
            reason,
            sample_rate: doubao::SAMPLE_RATE,
        }
    }

    /// Open a dictation, replacing any that was still open.
    pub fn begin(&mut self) -> Result<SttCloudBegin, String> {
        let key = credentials::api_key(CLOUD_PROVIDER_ID)
            .ok_or_else(|| "尚未配置豆包 API Key".to_string())?;
        // One dictation at a time, exactly like the daemon's `begin`: an open session
        // is closed rather than left to consume the microphone's bytes.
        self.sessions.clear();
        let session = CloudSession::connect(
            key,
            doubao::DEFAULT_ENDPOINT,
            doubao::DEFAULT_RESOURCE_ID,
            doubao::CloudTuning::default(),
        )?;
        self.next_id += 1;
        let session_id = self.next_id;
        self.sessions.insert(session_id, session);
        Ok(SttCloudBegin {
            session_id,
            sample_rate: doubao::SAMPLE_RATE,
        })
    }

    /// Feed one chunk of PCM to an open dictation.
    ///
    /// An unknown id is a no-op with an empty result: the console always awaits each
    /// append, so the only way to reach this is a dictation that already finished or
    /// was cancelled, and resurrecting it would be worse than ignoring it.
    pub fn append(&mut self, session_id: u64, data_base64: &str) -> SttCloudAppend {
        let Some(session) = self.sessions.get_mut(&session_id) else {
            return SttCloudAppend::default();
        };
        let update = session.append(data_base64);
        SttCloudAppend {
            partial: update.partial,
            finalized: update.finalized,
            error: session.error(),
        }
    }

    /// Remove a dictation so its caller can close it without holding this lock.
    pub fn take(&mut self, session_id: u64) -> Option<CloudSession> {
        self.sessions.remove(&session_id)
    }

    /// Tear a dictation down.  Dropping the session signals its socket worker.
    pub fn cancel(&mut self, session_id: u64) -> bool {
        self.sessions.remove(&session_id).is_some()
    }

    /// How many dictations are open (the console expects at most one).
    #[cfg(test)]
    pub fn open_sessions(&self) -> usize {
        self.sessions.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unknown_session_id_is_a_no_op() {
        let mut manager = SttManager::new();
        let result = manager.append(99, "");
        assert!(result.partial.is_empty());
        assert!(result.finalized.is_empty());
        assert!(result.error.is_none());
        assert!(!manager.cancel(99));
        assert!(manager.take(99).is_none());
        assert_eq!(manager.open_sessions(), 0);
    }

    #[test]
    fn status_reports_the_missing_key_rather_than_failing() {
        // The real home may or may not hold a key; what matters is that the shape is
        // always answerable and the sample rate is the engine's own.
        let status = SttManager::status();
        assert_eq!(status.sample_rate, doubao::SAMPLE_RATE);
        assert_eq!(status.available, status.key_configured);
        if !status.key_configured {
            assert!(
                status.reason.is_some(),
                "an unusable engine must explain itself"
            );
        }
    }

    #[test]
    fn begin_without_a_key_explains_itself() {
        if credentials::configured(CLOUD_PROVIDER_ID) {
            return; // a machine with a real key: nothing to assert here
        }
        let mut manager = SttManager::new();
        let err = manager.begin().expect_err("no key means no session");
        assert!(err.contains("API Key"));
        assert_eq!(manager.open_sessions(), 0);
    }
}
