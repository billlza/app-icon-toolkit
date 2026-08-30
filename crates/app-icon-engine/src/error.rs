use std::{io, path::PathBuf};

use app_icon_domain::{DomainError, RelativePath};
use thiserror::Error;

/// Observed namespace result for an icon-set publication attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PublicationState {
    /// Reconciliation proved that this invocation did not publish its staging directory.
    NotPublished,
    /// The filesystem state could not prove either outcome.
    Indeterminate,
}

/// Safe caller action after a publication-related failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RetryAdvice {
    /// A new attempt is safe because the previous attempt was proven not to have published.
    MayRetry,
    /// A new attempt would be incorrect because publication is already known to have occurred.
    DoNotRetry,
    /// Inspect the named output and staging paths before deciding whether another attempt is safe.
    ReconcileFirst,
}

/// Failures raised while inspecting, planning, rendering, or publishing icons.
#[derive(Debug, Error)]
pub enum EngineError {
    /// A validated domain value or plan could not be constructed.
    #[error(transparent)]
    Domain(#[from] DomainError),

    /// The ambient workspace root could not be converted into a capability.
    #[error("failed to open workspace root `{path}`: {source}")]
    WorkspaceRoot {
        /// Ambient path supplied by the trusted host.
        path: PathBuf,
        /// Operating-system failure.
        source: io::Error,
    },

    /// Source metadata or bytes could not be read.
    #[error("failed to read source `{path}`: {source}")]
    SourceRead {
        /// Workspace-relative source path.
        path: RelativePath,
        /// Operating-system failure.
        source: io::Error,
    },

    /// A source path does not identify a regular file.
    #[error("source `{path}` is not a regular file")]
    SourceNotRegular {
        /// Rejected source path.
        path: RelativePath,
    },

    /// A source exceeds the encoded-byte limit.
    #[error("source `{path}` is {actual_bytes} bytes; the limit is {maximum_bytes} bytes")]
    SourceTooLarge {
        /// Rejected source path.
        path: RelativePath,
        /// Observed encoded length.
        actual_bytes: u64,
        /// Configured encoded length limit.
        maximum_bytes: u64,
    },

    /// Source bytes are not a supported PNG stream.
    #[error("source `{path}` is not a supported PNG file")]
    UnsupportedSourceFormat {
        /// Rejected source path.
        path: RelativePath,
    },

    /// Animated PNG input is outside the deterministic single-frame contract.
    #[error("source `{path}` is animated; APNG input is unsupported")]
    AnimatedPngUnsupported {
        /// Rejected source path.
        path: RelativePath,
    },

    /// The PNG decoder rejected a source.
    #[error("failed to decode PNG source `{path}`: {source}")]
    ImageDecode {
        /// Rejected source path.
        path: RelativePath,
        /// Decoder failure.
        source: image::ImageError,
    },

    /// Decoded dimensions violate a source-role invariant.
    #[error("source `{path}` has invalid dimensions {width}x{height}: {requirement}")]
    InvalidSourceDimensions {
        /// Rejected source path.
        path: RelativePath,
        /// Decoded width.
        width: u32,
        /// Decoded height.
        height: u32,
        /// Stable requirement description.
        requirement: &'static str,
    },

    /// An Android adaptive background contains transparent pixels.
    #[error("android adaptive background `{path}` must be fully opaque")]
    AdaptiveBackgroundNotOpaque {
        /// Rejected background path.
        path: RelativePath,
    },

    /// The requested output parent cannot be opened as a capability directory.
    #[error("failed to open output parent `{path}`: {source}")]
    OutputParent {
        /// Workspace-relative output parent.
        path: RelativePath,
        /// Operating-system failure.
        source: io::Error,
    },

    /// The no-overwrite contract rejected an existing output path.
    #[error("output directory `{path}` already exists")]
    OutputExists {
        /// Existing output directory.
        path: RelativePath,
    },

    /// A sibling staging directory could not be created.
    #[error("failed to create staging directory for `{path}`: {source}")]
    StagingCreate {
        /// Intended final output directory.
        path: RelativePath,
        /// Operating-system failure.
        source: io::Error,
    },

    /// The newly created staging directory could not be assigned a stable
    /// filesystem identity, so it is preserved instead of being deleted by name.
    #[error(
        "failed to identify staging directory `{staging_path}` for `{path}`; it was preserved: {source}"
    )]
    StagingIdentity {
        /// Intended final output directory.
        path: RelativePath,
        /// Workspace-relative staging directory that may require inspection.
        staging_path: RelativePath,
        /// Filesystem identity lookup failure.
        source: io::Error,
    },

    /// An artifact directory could not be created inside staging.
    #[error("failed to create artifact parent for `{path}`: {source}")]
    ArtifactParent {
        /// Artifact path relative to the output directory.
        path: RelativePath,
        /// Operating-system failure.
        source: io::Error,
    },

    /// An artifact could not be created with exclusive-create semantics.
    #[error("failed to create artifact `{path}`: {source}")]
    ArtifactCreate {
        /// Artifact path relative to the output directory.
        path: RelativePath,
        /// Operating-system failure.
        source: io::Error,
    },

    /// Encoded artifact bytes could not be written or synchronized.
    #[error("failed to write artifact `{path}`: {source}")]
    ArtifactWrite {
        /// Artifact path relative to the output directory.
        path: RelativePath,
        /// Operating-system failure.
        source: io::Error,
    },

    /// Generated artifact bytes could not be read back from staging.
    #[error("failed to read generated artifact `{path}`: {source}")]
    ArtifactRead {
        /// Artifact path relative to the output directory.
        path: RelativePath,
        /// Operating-system failure.
        source: io::Error,
    },

    /// Platform artifact encoding failed before publication.
    #[error("failed to encode artifact `{path}`: {source}")]
    ArtifactEncode {
        /// Artifact path relative to the output directory.
        path: RelativePath,
        /// Encoder failure.
        source: io::Error,
    },

    /// Generated metadata serialization failed.
    #[error("failed to serialize artifact `{path}`: {source}")]
    ArtifactSerialize {
        /// Artifact path relative to the output directory.
        path: RelativePath,
        /// JSON serialization failure.
        source: serde_json::Error,
    },

    /// Generated bytes failed their post-write verification.
    #[error("generated artifact `{path}` failed validation: {reason}")]
    ArtifactValidation {
        /// Invalid artifact path relative to the output directory.
        path: RelativePath,
        /// Specific verification failure.
        reason: String,
    },

    /// The host cannot provide an atomic no-replace directory publication.
    #[error("atomic no-replace publication is unsupported for `{path}` on this host: {reason}")]
    AtomicPublishUnsupported {
        /// Intended output directory.
        path: RelativePath,
        /// Stable explanation of the missing primitive.
        reason: String,
    },

    /// The atomic staging-to-final rename failed.
    #[error("failed to publish output directory `{path}` atomically: {source}")]
    Publish {
        /// Intended output directory.
        path: RelativePath,
        /// Operating-system failure.
        source: io::Error,
    },

    /// The native rename result and subsequent identity observations could not
    /// prove whether the staging directory was published.
    #[error(
        "publication outcome for `{path}` is indeterminate ({native_result}); preserve `{staging_path}` if present and reconcile before retrying: {reconciliation_reason}"
    )]
    PublishOutcomeIndeterminate {
        /// Intended final output directory.
        path: RelativePath,
        /// Workspace-relative sibling staging path that may still exist.
        staging_path: RelativePath,
        /// Native success or error result retained for diagnosis.
        native_result: String,
        /// Stable code of the underlying publication error, when there was one.
        primary_code: Option<&'static str>,
        /// Exact observations that prevented a definitive result.
        reconciliation_reason: String,
    },

    /// Generation failed before publication was proven, so staging was
    /// preserved instead of being removed through a raceable name.
    #[error(
        "generation failed ({primary}); staging directory `{staging_path}` was preserved for safe inspection"
    )]
    StagingPreserved {
        /// Intended final output directory.
        path: RelativePath,
        /// Workspace-relative staging path retained for inspection or explicit cleanup.
        staging_path: RelativePath,
        /// Typed primary failure retained for its stable code and context.
        primary: Box<EngineError>,
    },
}

impl EngineError {
    /// Returns a stable machine-readable error code for MCP mapping.
    #[must_use]
    pub fn code(&self) -> &'static str {
        match self {
            Self::Domain(_) => "DOMAIN_ERROR",
            Self::WorkspaceRoot { .. } => "WORKSPACE_ROOT_UNAVAILABLE",
            Self::SourceRead { .. } => "SOURCE_READ_FAILED",
            Self::SourceNotRegular { .. } => "SOURCE_NOT_REGULAR",
            Self::SourceTooLarge { .. } => "SOURCE_TOO_LARGE",
            Self::UnsupportedSourceFormat { .. } => "UNSUPPORTED_SOURCE_FORMAT",
            Self::AnimatedPngUnsupported { .. } => "ANIMATED_PNG_UNSUPPORTED",
            Self::ImageDecode { .. } => "SOURCE_DECODE_FAILED",
            Self::InvalidSourceDimensions { .. } => "INVALID_SOURCE_DIMENSIONS",
            Self::AdaptiveBackgroundNotOpaque { .. } => "ADAPTIVE_BACKGROUND_NOT_OPAQUE",
            Self::OutputParent { .. } => "OUTPUT_PARENT_UNAVAILABLE",
            Self::OutputExists { .. } => "OUTPUT_EXISTS",
            Self::StagingCreate { .. } => "STAGING_CREATE_FAILED",
            Self::StagingIdentity { .. } => "STAGING_IDENTITY_FAILED",
            Self::ArtifactParent { .. } => "ARTIFACT_PARENT_FAILED",
            Self::ArtifactCreate { .. } => "ARTIFACT_CREATE_FAILED",
            Self::ArtifactWrite { .. } => "ARTIFACT_WRITE_FAILED",
            Self::ArtifactRead { .. } => "ARTIFACT_READ_FAILED",
            Self::ArtifactEncode { .. } => "ARTIFACT_ENCODE_FAILED",
            Self::ArtifactSerialize { .. } => "ARTIFACT_SERIALIZE_FAILED",
            Self::ArtifactValidation { .. } => "ARTIFACT_VALIDATION_FAILED",
            Self::AtomicPublishUnsupported { .. } => "ATOMIC_PUBLISH_UNSUPPORTED",
            Self::Publish { .. } => "ATOMIC_PUBLISH_FAILED",
            Self::PublishOutcomeIndeterminate { .. } => "ATOMIC_PUBLISH_INDETERMINATE",
            Self::StagingPreserved { primary, .. } => primary.code(),
        }
    }

    /// Formats the complete user-facing error message.
    #[must_use]
    pub fn message(&self) -> String {
        self.to_string()
    }

    /// Returns the workspace-relative path associated with the failure.
    #[must_use]
    pub fn relative_path(&self) -> Option<&RelativePath> {
        match self {
            Self::Domain(_) | Self::WorkspaceRoot { .. } => None,
            Self::SourceRead { path, .. }
            | Self::SourceNotRegular { path }
            | Self::SourceTooLarge { path, .. }
            | Self::UnsupportedSourceFormat { path }
            | Self::AnimatedPngUnsupported { path }
            | Self::ImageDecode { path, .. }
            | Self::InvalidSourceDimensions { path, .. }
            | Self::AdaptiveBackgroundNotOpaque { path }
            | Self::OutputParent { path, .. }
            | Self::OutputExists { path }
            | Self::StagingCreate { path, .. }
            | Self::StagingIdentity { path, .. }
            | Self::ArtifactParent { path, .. }
            | Self::ArtifactCreate { path, .. }
            | Self::ArtifactWrite { path, .. }
            | Self::ArtifactRead { path, .. }
            | Self::ArtifactEncode { path, .. }
            | Self::ArtifactSerialize { path, .. }
            | Self::ArtifactValidation { path, .. }
            | Self::AtomicPublishUnsupported { path, .. }
            | Self::Publish { path, .. }
            | Self::PublishOutcomeIndeterminate { path, .. } => Some(path),
            Self::StagingPreserved { path, primary, .. } => primary.relative_path().or(Some(path)),
        }
    }

    /// Returns the namespace state when this error carries publication outcome information.
    #[must_use]
    pub fn publication_state(&self) -> Option<PublicationState> {
        match self {
            Self::OutputExists { .. }
            | Self::StagingIdentity { .. }
            | Self::AtomicPublishUnsupported { .. }
            | Self::Publish { .. }
            | Self::StagingPreserved { .. } => Some(PublicationState::NotPublished),
            Self::PublishOutcomeIndeterminate { .. } => Some(PublicationState::Indeterminate),
            _ => None,
        }
    }

    /// Returns the action a caller may take without risking a duplicate publication.
    #[must_use]
    pub fn retry_advice(&self) -> Option<RetryAdvice> {
        match self {
            Self::StagingIdentity { .. } | Self::Publish { .. } => Some(RetryAdvice::MayRetry),
            Self::OutputExists { .. } | Self::AtomicPublishUnsupported { .. } => {
                Some(RetryAdvice::DoNotRetry)
            }
            Self::PublishOutcomeIndeterminate { .. } => Some(RetryAdvice::ReconcileFirst),
            Self::StagingPreserved { primary, .. } => {
                primary.retry_advice().or(Some(RetryAdvice::MayRetry))
            }
            _ => None,
        }
    }

    /// Returns the sibling staging path when it is needed for manual reconciliation.
    #[must_use]
    pub const fn staging_relative_path(&self) -> Option<&RelativePath> {
        match self {
            Self::StagingIdentity { staging_path, .. }
            | Self::PublishOutcomeIndeterminate { staging_path, .. }
            | Self::StagingPreserved { staging_path, .. } => Some(staging_path),
            _ => None,
        }
    }

    /// Returns the typed primary error code retained by a wrapper error.
    #[must_use]
    pub fn primary_code(&self) -> Option<&'static str> {
        match self {
            Self::PublishOutcomeIndeterminate { primary_code, .. } => *primary_code,
            Self::StagingPreserved { primary, .. } => Some(primary.code()),
            _ => None,
        }
    }
}
