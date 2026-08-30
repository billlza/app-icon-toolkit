//! Pure domain contracts for application icon planning.
//!
//! This crate intentionally contains no MCP, async runtime, image codec, or
//! filesystem implementation dependencies.

mod error;
mod model;
mod path;
mod plan;

pub use error::DomainError;
pub use model::{
    AdaptiveSources, AndroidResourceName, ApplicationId, ArtifactName, DisplayName, ExecutableName,
    IconJob, IconSources, PlatformProfile, TargetSpec,
};
pub use path::RelativePath;
pub use plan::{ArtifactKind, ArtifactPlan, IconPlan, ProfilePlan, SourceInspection};

/// Largest accepted encoded source file.
pub const MAX_SOURCE_BYTES: u64 = 64 * 1024 * 1024;

/// Largest accepted source-image edge.
pub const MAX_SOURCE_EDGE: u32 = 4_096;

/// Minimum flattened source edge needed by the current profiles.
pub const MIN_FLATTENED_EDGE: u32 = 1_024;

/// Minimum Android adaptive layer edge for the xxxhdpi 108dp canvas.
pub const MIN_ADAPTIVE_EDGE: u32 = 432;
