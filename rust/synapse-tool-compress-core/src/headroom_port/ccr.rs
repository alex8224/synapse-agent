// SPDX-License-Identifier: Apache-2.0
//
// Minimal contract derived from Headroom's `crates/headroom-core/src/ccr/mod.rs`.
// Synapse owns persistent reversible storage; this port accepts a store only
// through this trait and does not include Headroom's Redis or SQLite backends.

use std::time::Duration;

pub use super::ccr_in_memory::InMemoryCcrStore;

pub const DEFAULT_CAPACITY: usize = 1000;
pub const DEFAULT_TTL: Duration = Duration::from_secs(1800);

pub trait CcrStore: Send + Sync {
    fn put(&self, hash: &str, payload: &str);
    fn get(&self, hash: &str) -> Option<String>;
    fn len(&self) -> usize;

    fn is_empty(&self) -> bool {
        self.len() == 0
    }
}
