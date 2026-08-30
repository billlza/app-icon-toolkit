//! Icon planning, rendering, validation, and transactional publication.
//!
//! The engine is deliberately independent from MCP. Callers grant access to
//! one workspace root, and every source and output path is resolved relative
//! to that capability.

mod error;
mod exporters;
mod render;
mod report;
mod service;
mod source;
mod transaction;

pub use error::{EngineError, PublicationState, RetryAdvice};
pub use report::GenerationReport;
pub use service::IconService;
