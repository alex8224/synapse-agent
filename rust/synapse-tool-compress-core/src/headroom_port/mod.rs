// SPDX-License-Identifier: Apache-2.0
//
// This module contains selected source files copied from Headroom under the
// Apache License 2.0. See ../../LICENSE and ../../NOTICE.
//
// Synapse modifications remove optional ML, networking, proxy and external
// CCR-backend integration. The retained modules are deterministic transforms.

pub mod ccr;
mod ccr_in_memory;

pub mod relevance {
    pub mod base;
    pub mod bm25;

    pub use base::{RelevanceScore, RelevanceScorer};
    pub use bm25::BM25Scorer;
}

pub mod signals {
    pub mod keyword_detector;
    pub mod line_importance;

    pub use keyword_detector::KeywordDetector;
    pub use line_importance::{
        ImportanceCategory, ImportanceContext, ImportanceSignal, LineImportanceDetector,
    };
}

pub mod transforms {
    pub mod adaptive_sizer;
    pub mod anchor_selector;
    pub mod code_compressor;
    pub mod diff_compressor;
    pub mod log_compressor;
    pub mod search_compressor;
    pub mod smart_crusher;
}
