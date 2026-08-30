use thiserror::Error;

use crate::PlatformProfile;

/// Validation failures for domain values and plans.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum DomainError {
    /// A caller supplied an unsafe or non-portable relative path.
    #[error("invalid relative path `{value}`: {reason}")]
    InvalidRelativePath {
        /// Rejected value.
        value: String,
        /// Stable human-readable reason.
        reason: &'static str,
    },

    /// A caller supplied an invalid platform identifier.
    #[error("invalid {field} `{value}`: {reason}")]
    InvalidIdentifier {
        /// Name of the rejected field.
        field: &'static str,
        /// Rejected value.
        value: String,
        /// Stable human-readable reason.
        reason: &'static str,
    },

    /// A job did not request any target profile.
    #[error("at least one target profile is required")]
    EmptyTargets,

    /// A job requested the same profile more than once.
    #[error("duplicate target profile `{profile:?}`")]
    DuplicateTarget {
        /// Duplicated profile.
        profile: PlatformProfile,
    },

    /// Android adaptive output was requested without semantic layers.
    #[error("android adaptive output requires explicit foreground and background sources")]
    MissingAdaptiveSources,

    /// Two artifact recipes resolve to the same portable path.
    #[error("artifact path collision between `{first}` and `{second}`")]
    ArtifactPathCollision {
        /// First colliding path.
        first: String,
        /// Second colliding path.
        second: String,
    },
}
