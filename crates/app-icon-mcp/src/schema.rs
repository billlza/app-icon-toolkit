use std::path::{Component, PathBuf};

use app_icon_domain::{
    AdaptiveSources, AndroidResourceName, ApplicationId, ArtifactKind, ArtifactName, ArtifactPlan,
    DisplayName, ExecutableName, IconJob, IconPlan, IconSources, PlatformProfile, ProfilePlan,
    RelativePath, SourceInspection, TargetSpec,
};
use app_icon_engine::{EngineError, PublicationState, RetryAdvice};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub(crate) struct IconSetRequest {
    /// Absolute directory that contains every source and the requested output directory.
    workspace_root: String,
    /// New output directory relative to `workspace_root`.
    output_directory: String,
    /// PNG source artwork paths relative to `workspace_root`.
    sources: IconSourcesRequest,
    /// Platform profiles to plan or generate. A profile may appear only once.
    targets: Vec<TargetRequest>,
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct IconSourcesRequest {
    /// Flattened square PNG master used by every non-adaptive output.
    flattened: String,
    /// Explicit semantic artwork required by the Android adaptive profile.
    adaptive: Option<AdaptiveSourcesRequest>,
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct AdaptiveSourcesRequest {
    /// Android adaptive foreground-layer PNG.
    foreground: String,
    /// Android adaptive background-layer PNG.
    background: String,
    /// Optional Android 13 monochrome-layer PNG.
    monochrome: Option<String>,
}

#[derive(Debug, Deserialize, JsonSchema)]
#[serde(tag = "profile", rename_all = "snake_case", deny_unknown_fields)]
enum TargetRequest {
    /// Generate a macOS app icon set for an Xcode asset catalog.
    MacOsAppIconSet {
        /// App icon set name without the `.appiconset` extension.
        icon_set_name: String,
    },
    /// Generate Android legacy and adaptive launcher resources.
    AndroidAdaptive {
        /// Android resource name, for example `ic_launcher`.
        resource_name: String,
    },
    /// Generate a multi-frame Win32 ICO file.
    WindowsIco {
        /// ICO filename stem without the extension.
        file_stem: String,
    },
    /// Generate the fixed Windows MSIX application icon asset matrix.
    WindowsMsixAssets,
    /// Generate a freedesktop hicolor tree and desktop entry.
    LinuxXdg {
        /// Reverse-domain application identifier.
        application_id: String,
        /// User-facing application name.
        display_name: String,
        /// Executable name without arguments or a path.
        executable: String,
    },
}

pub(crate) struct ToolInvocation {
    pub(crate) workspace_root: PathBuf,
    pub(crate) job: IconJob,
}

impl TryFrom<IconSetRequest> for ToolInvocation {
    type Error = ToolFailure;

    fn try_from(request: IconSetRequest) -> Result<Self, Self::Error> {
        let workspace_root = validate_workspace_root(request.workspace_root)?;
        let output_directory = relative_path("output_directory", request.output_directory)?;
        let flattened = relative_path("sources.flattened", request.sources.flattened)?;

        let adaptive = request
            .sources
            .adaptive
            .map(|adaptive| {
                let foreground = relative_path("sources.adaptive.foreground", adaptive.foreground)?;
                let background = relative_path("sources.adaptive.background", adaptive.background)?;
                let monochrome = adaptive
                    .monochrome
                    .map(|path| relative_path("sources.adaptive.monochrome", path))
                    .transpose()?;
                Ok(AdaptiveSources::new(foreground, background, monochrome))
            })
            .transpose()?;

        let targets = request
            .targets
            .into_iter()
            .map(TargetRequest::try_into_domain)
            .collect::<Result<Vec<_>, _>>()?;

        let sources = IconSources::new(flattened, adaptive);
        let job = IconJob::new(output_directory, sources, targets)
            .map_err(|error| ToolFailure::invalid_request("targets", error.to_string()))?;

        Ok(Self {
            workspace_root,
            job,
        })
    }
}

impl TargetRequest {
    fn try_into_domain(self) -> Result<TargetSpec, ToolFailure> {
        match self {
            Self::MacOsAppIconSet { icon_set_name } => {
                let icon_set_name = ArtifactName::new(icon_set_name).map_err(|error| {
                    ToolFailure::invalid_request("targets.icon_set_name", error.to_string())
                })?;
                Ok(TargetSpec::MacOsAppIconSet { icon_set_name })
            }
            Self::AndroidAdaptive { resource_name } => {
                let resource_name = AndroidResourceName::new(resource_name).map_err(|error| {
                    ToolFailure::invalid_request("targets.resource_name", error.to_string())
                })?;
                Ok(TargetSpec::AndroidAdaptive { resource_name })
            }
            Self::WindowsIco { file_stem } => {
                let file_stem = ArtifactName::new(file_stem).map_err(|error| {
                    ToolFailure::invalid_request("targets.file_stem", error.to_string())
                })?;
                Ok(TargetSpec::WindowsIco { file_stem })
            }
            Self::WindowsMsixAssets => Ok(TargetSpec::WindowsMsixAssets),
            Self::LinuxXdg {
                application_id,
                display_name,
                executable,
            } => {
                let application_id = ApplicationId::new(application_id).map_err(|error| {
                    ToolFailure::invalid_request("targets.application_id", error.to_string())
                })?;
                let display_name = DisplayName::new(display_name).map_err(|error| {
                    ToolFailure::invalid_request("targets.display_name", error.to_string())
                })?;
                let executable = ExecutableName::new(executable).map_err(|error| {
                    ToolFailure::invalid_request("targets.executable", error.to_string())
                })?;
                Ok(TargetSpec::LinuxXdg {
                    application_id,
                    display_name,
                    executable,
                })
            }
        }
    }
}

fn validate_workspace_root(value: String) -> Result<PathBuf, ToolFailure> {
    if value.chars().any(char::is_control) {
        return Err(ToolFailure::invalid_request(
            "workspace_root",
            "workspace root must not contain control characters",
        ));
    }
    let path = PathBuf::from(&value);
    if !path.is_absolute() {
        return Err(ToolFailure::invalid_request(
            "workspace_root",
            "workspace root must be an absolute path",
        ));
    }
    if path
        .components()
        .any(|component| matches!(component, Component::CurDir | Component::ParentDir))
    {
        return Err(ToolFailure::invalid_request(
            "workspace_root",
            "workspace root must not contain dot path components",
        ));
    }
    Ok(path)
}

fn relative_path(field: &'static str, value: String) -> Result<RelativePath, ToolFailure> {
    RelativePath::new(value).map_err(|error| ToolFailure::invalid_request(field, error.to_string()))
}

#[derive(Debug, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub(crate) struct PlanIconSetResponse {
    output_directory: String,
    sources: Vec<SourceResponse>,
    profiles: Vec<ProfileResponse>,
}

impl From<&IconPlan> for PlanIconSetResponse {
    fn from(plan: &IconPlan) -> Self {
        Self {
            output_directory: plan.output_directory().to_string(),
            sources: plan.sources().iter().map(SourceResponse::from).collect(),
            profiles: plan.profiles().iter().map(ProfileResponse::from).collect(),
        }
    }
}

#[derive(Debug, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct SourceResponse {
    path: String,
    width: u32,
    height: u32,
    has_alpha: bool,
    opaque: bool,
}

impl From<&SourceInspection> for SourceResponse {
    fn from(source: &SourceInspection) -> Self {
        Self {
            path: source.path().to_string(),
            width: source.width(),
            height: source.height(),
            has_alpha: source.has_alpha(),
            opaque: source.opaque(),
        }
    }
}

#[derive(Debug, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct ProfileResponse {
    profile: ProfileName,
    artifacts: Vec<ArtifactResponse>,
}

impl From<&ProfilePlan> for ProfileResponse {
    fn from(profile: &ProfilePlan) -> Self {
        Self {
            profile: profile.profile().into(),
            artifacts: profile
                .artifacts()
                .iter()
                .map(ArtifactResponse::from)
                .collect(),
        }
    }
}

#[derive(Debug, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
enum ProfileName {
    MacOsAppIconSet,
    AndroidAdaptive,
    WindowsIco,
    WindowsMsixAssets,
    LinuxXdg,
}

impl From<PlatformProfile> for ProfileName {
    fn from(profile: PlatformProfile) -> Self {
        match profile {
            PlatformProfile::MacOsAppIconSet => Self::MacOsAppIconSet,
            PlatformProfile::AndroidAdaptive => Self::AndroidAdaptive,
            PlatformProfile::WindowsIco => Self::WindowsIco,
            PlatformProfile::WindowsMsixAssets => Self::WindowsMsixAssets,
            PlatformProfile::LinuxXdg => Self::LinuxXdg,
        }
    }
}

#[derive(Debug, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub(crate) struct GenerateIconSetResponse {
    output_directory: String,
    artifacts: Vec<ArtifactResponse>,
}

impl GenerateIconSetResponse {
    pub(crate) fn new(output_directory: &RelativePath, artifacts: &[ArtifactPlan]) -> Self {
        Self {
            output_directory: output_directory.to_string(),
            artifacts: artifacts.iter().map(ArtifactResponse::from).collect(),
        }
    }
}

#[derive(Debug, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
struct ArtifactResponse {
    path: String,
    kind: ArtifactKindName,
    #[serde(skip_serializing_if = "Option::is_none")]
    pixel_width: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pixel_height: Option<u32>,
}

impl From<&ArtifactPlan> for ArtifactResponse {
    fn from(artifact: &ArtifactPlan) -> Self {
        Self {
            path: artifact.path().to_string(),
            kind: artifact.kind().into(),
            pixel_width: artifact.pixel_width(),
            pixel_height: artifact.pixel_height(),
        }
    }
}

#[derive(Debug, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
enum ArtifactKindName {
    Png,
    Json,
    Xml,
    Ico,
    DesktopEntry,
}

impl From<ArtifactKind> for ArtifactKindName {
    fn from(kind: ArtifactKind) -> Self {
        match kind {
            ArtifactKind::Png => Self::Png,
            ArtifactKind::Json => Self::Json,
            ArtifactKind::Xml => Self::Xml,
            ArtifactKind::Ico => Self::Ico,
            ArtifactKind::DesktopEntry => Self::DesktopEntry,
        }
    }
}

#[derive(Debug, Serialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub(crate) struct ToolFailure {
    code: String,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    relative_path: Option<String>,
    #[serde(flatten, skip_serializing_if = "Option::is_none")]
    publication: Option<Box<PublicationFailureContext>>,
}

#[derive(Debug, Serialize, JsonSchema)]
struct PublicationFailureContext {
    #[serde(skip_serializing_if = "Option::is_none")]
    publication_state: Option<PublicationStateName>,
    #[serde(skip_serializing_if = "Option::is_none")]
    retry_advice: Option<RetryAdviceName>,
    #[serde(skip_serializing_if = "Option::is_none")]
    staging_relative_path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    primary_code: Option<String>,
}

#[derive(Debug, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
enum PublicationStateName {
    NotPublished,
    Indeterminate,
}

impl From<PublicationState> for PublicationStateName {
    fn from(state: PublicationState) -> Self {
        match state {
            PublicationState::NotPublished => Self::NotPublished,
            PublicationState::Indeterminate => Self::Indeterminate,
        }
    }
}

#[derive(Debug, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
enum RetryAdviceName {
    MayRetry,
    DoNotRetry,
    ReconcileFirst,
}

impl From<RetryAdvice> for RetryAdviceName {
    fn from(advice: RetryAdvice) -> Self {
        match advice {
            RetryAdvice::MayRetry => Self::MayRetry,
            RetryAdvice::DoNotRetry => Self::DoNotRetry,
            RetryAdvice::ReconcileFirst => Self::ReconcileFirst,
        }
    }
}

impl ToolFailure {
    pub(crate) fn invalid_request(field: &'static str, message: impl Into<String>) -> Self {
        Self {
            code: "INVALID_REQUEST".to_owned(),
            message: format!("invalid `{field}`: {}", message.into()),
            relative_path: None,
            publication: None,
        }
    }

    pub(crate) fn busy(operation: &'static str) -> Self {
        Self {
            code: "BUSY".to_owned(),
            message: format!("the server is at its concurrent {operation} limit; retry later"),
            relative_path: None,
            publication: None,
        }
    }

    pub(crate) fn internal(message: impl Into<String>) -> Self {
        Self {
            code: "INTERNAL".to_owned(),
            message: message.into(),
            relative_path: None,
            publication: None,
        }
    }

    pub(crate) fn engine(error: &EngineError) -> Self {
        Self {
            code: error.code().to_owned(),
            message: error.message(),
            relative_path: error.relative_path().map(ToString::to_string),
            publication: PublicationFailureContext::from_error(error).map(Box::new),
        }
    }
}

impl PublicationFailureContext {
    fn from_error(error: &EngineError) -> Option<Self> {
        let context = Self {
            publication_state: error.publication_state().map(Into::into),
            retry_advice: error.retry_advice().map(Into::into),
            staging_relative_path: error.staging_relative_path().map(ToString::to_string),
            primary_code: error.primary_code().map(str::to_owned),
        };
        if context.publication_state.is_none()
            && context.retry_advice.is_none()
            && context.staging_relative_path.is_none()
            && context.primary_code.is_none()
        {
            None
        } else {
            Some(context)
        }
    }
}

#[cfg(test)]
mod tests {
    use app_icon_domain::RelativePath;
    use app_icon_engine::EngineError;

    use super::ToolFailure;

    #[test]
    fn indeterminate_publication_failure_requires_reconciliation_before_retry() {
        let output = match RelativePath::new("generated") {
            Ok(path) => path,
            Err(error) => panic!("test output path is invalid: {error}"),
        };
        let staging = match RelativePath::new(".app-icon-toolkit-staging-1-2-3") {
            Ok(path) => path,
            Err(error) => panic!("test staging path is invalid: {error}"),
        };
        let error = EngineError::PublishOutcomeIndeterminate {
            path: output,
            staging_path: staging,
            native_result: "injected I/O error".to_owned(),
            primary_code: Some("ATOMIC_PUBLISH_FAILED"),
            reconciliation_reason: "both names were unobservable".to_owned(),
        };

        let value = match serde_json::to_value(ToolFailure::engine(&error)) {
            Ok(value) => value,
            Err(error) => panic!("tool failure did not serialize: {error}"),
        };

        assert_eq!(
            value.get("code").and_then(serde_json::Value::as_str),
            Some("ATOMIC_PUBLISH_INDETERMINATE")
        );
        assert_eq!(
            value
                .get("publication_state")
                .and_then(serde_json::Value::as_str),
            Some("indeterminate")
        );
        assert_eq!(
            value
                .get("retry_advice")
                .and_then(serde_json::Value::as_str),
            Some("reconcile_first")
        );
        assert_eq!(
            value
                .get("staging_relative_path")
                .and_then(serde_json::Value::as_str),
            Some(".app-icon-toolkit-staging-1-2-3")
        );
        assert_eq!(
            value
                .get("primary_code")
                .and_then(serde_json::Value::as_str),
            Some("ATOMIC_PUBLISH_FAILED")
        );
    }

    #[test]
    fn preserved_collision_retains_primary_code_and_forbids_blind_retry() {
        let output = match RelativePath::new("generated") {
            Ok(path) => path,
            Err(error) => panic!("test output path is invalid: {error}"),
        };
        let staging = match RelativePath::new(".app-icon-toolkit-staging-1-2-3") {
            Ok(path) => path,
            Err(error) => panic!("test staging path is invalid: {error}"),
        };
        let error = EngineError::StagingPreserved {
            path: output.clone(),
            staging_path: staging,
            primary: Box::new(EngineError::OutputExists { path: output }),
        };

        let value = match serde_json::to_value(ToolFailure::engine(&error)) {
            Ok(value) => value,
            Err(error) => panic!("tool failure did not serialize: {error}"),
        };

        assert_eq!(
            value.get("code").and_then(serde_json::Value::as_str),
            Some("OUTPUT_EXISTS")
        );
        assert_eq!(
            value
                .get("publication_state")
                .and_then(serde_json::Value::as_str),
            Some("not_published")
        );
        assert_eq!(
            value
                .get("retry_advice")
                .and_then(serde_json::Value::as_str),
            Some("do_not_retry")
        );
        assert_eq!(
            value
                .get("staging_relative_path")
                .and_then(serde_json::Value::as_str),
            Some(".app-icon-toolkit-staging-1-2-3")
        );
        assert_eq!(
            value
                .get("primary_code")
                .and_then(serde_json::Value::as_str),
            Some("OUTPUT_EXISTS")
        );
    }
}
