use std::collections::BTreeSet;

use serde::Serialize;

use crate::path::is_windows_device_name;
use crate::{DomainError, RelativePath};

const MAX_IDENTIFIER_BYTES: usize = 255;

/// Validated paths for Android adaptive icon semantics.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AdaptiveSources {
    foreground: RelativePath,
    background: RelativePath,
    monochrome: Option<RelativePath>,
}

impl AdaptiveSources {
    /// Creates a semantic adaptive source set.
    #[must_use]
    pub const fn new(
        foreground: RelativePath,
        background: RelativePath,
        monochrome: Option<RelativePath>,
    ) -> Self {
        Self {
            foreground,
            background,
            monochrome,
        }
    }

    /// Foreground layer path.
    #[must_use]
    pub const fn foreground(&self) -> &RelativePath {
        &self.foreground
    }

    /// Background layer path.
    #[must_use]
    pub const fn background(&self) -> &RelativePath {
        &self.background
    }

    /// Optional monochrome layer path.
    #[must_use]
    pub const fn monochrome(&self) -> Option<&RelativePath> {
        self.monochrome.as_ref()
    }
}

/// Validated source artwork references.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct IconSources {
    flattened: RelativePath,
    adaptive: Option<AdaptiveSources>,
}

impl IconSources {
    /// Creates a source set with a required flattened master.
    #[must_use]
    pub const fn new(flattened: RelativePath, adaptive: Option<AdaptiveSources>) -> Self {
        Self {
            flattened,
            adaptive,
        }
    }

    /// Flattened master path used by non-adaptive profiles and Android legacy output.
    #[must_use]
    pub const fn flattened(&self) -> &RelativePath {
        &self.flattened
    }

    /// Optional Android adaptive semantic sources.
    #[must_use]
    pub const fn adaptive(&self) -> Option<&AdaptiveSources> {
        self.adaptive.as_ref()
    }
}

/// Stable platform profile identifiers.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum PlatformProfile {
    /// Xcode macOS app icon set that can be placed in an asset catalog.
    MacOsAppIconSet,
    /// Android adaptive and legacy launcher resources.
    AndroidAdaptive,
    /// Multi-frame Windows Win32 ICO.
    WindowsIco,
    /// Windows MSIX application-list, medium-tile, and Store logo assets.
    WindowsMsixAssets,
    /// Freedesktop hicolor icon tree and desktop entry.
    LinuxXdg,
}

/// Target-specific generation options.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "profile", rename_all = "snake_case")]
pub enum TargetSpec {
    /// Xcode macOS app icon set.
    MacOsAppIconSet {
        /// App icon set directory name without the `.appiconset` extension.
        icon_set_name: ArtifactName,
    },
    /// Android adaptive and legacy launcher resources.
    AndroidAdaptive {
        /// Android resource identifier, such as `ic_launcher`.
        resource_name: AndroidResourceName,
    },
    /// Multi-frame Win32 ICO.
    WindowsIco {
        /// Output filename stem without `.ico`.
        file_stem: ArtifactName,
    },
    /// Fixed Windows MSIX application icon asset matrix.
    WindowsMsixAssets,
    /// Freedesktop hicolor tree and desktop entry.
    LinuxXdg {
        /// Reverse-domain desktop/application identifier.
        application_id: ApplicationId,
        /// User-facing desktop entry name.
        display_name: DisplayName,
        /// Executable name used by the desktop entry.
        executable: ExecutableName,
    },
}

impl TargetSpec {
    /// Returns the stable profile discriminator.
    #[must_use]
    pub const fn profile(&self) -> PlatformProfile {
        match self {
            Self::MacOsAppIconSet { .. } => PlatformProfile::MacOsAppIconSet,
            Self::AndroidAdaptive { .. } => PlatformProfile::AndroidAdaptive,
            Self::WindowsIco { .. } => PlatformProfile::WindowsIco,
            Self::WindowsMsixAssets => PlatformProfile::WindowsMsixAssets,
            Self::LinuxXdg { .. } => PlatformProfile::LinuxXdg,
        }
    }
}

/// Fully validated icon-generation job.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct IconJob {
    output_directory: RelativePath,
    sources: IconSources,
    targets: Vec<TargetSpec>,
}

impl IconJob {
    /// Validates cross-target invariants and constructs a job.
    pub fn new(
        output_directory: RelativePath,
        sources: IconSources,
        targets: Vec<TargetSpec>,
    ) -> Result<Self, DomainError> {
        if targets.is_empty() {
            return Err(DomainError::EmptyTargets);
        }

        let mut profiles = BTreeSet::new();
        for target in &targets {
            let profile = target.profile();
            if !profiles.insert(profile) {
                return Err(DomainError::DuplicateTarget { profile });
            }
            if profile == PlatformProfile::AndroidAdaptive && sources.adaptive().is_none() {
                return Err(DomainError::MissingAdaptiveSources);
            }
        }

        Ok(Self {
            output_directory,
            sources,
            targets,
        })
    }

    /// Workspace-relative output directory.
    #[must_use]
    pub const fn output_directory(&self) -> &RelativePath {
        &self.output_directory
    }

    /// Validated source set.
    #[must_use]
    pub const fn sources(&self) -> &IconSources {
        &self.sources
    }

    /// Requested target profiles.
    #[must_use]
    pub fn targets(&self) -> &[TargetSpec] {
        &self.targets
    }
}

macro_rules! identifier_type {
    ($name:ident, $field:literal, $validator:ident, $docs:literal) => {
        #[doc = $docs]
        #[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
        #[serde(transparent)]
        pub struct $name(String);

        impl $name {
            /// Validates and constructs the identifier.
            pub fn new(value: impl Into<String>) -> Result<Self, DomainError> {
                let value = value.into();
                $validator(&value).map_err(|reason| DomainError::InvalidIdentifier {
                    field: $field,
                    value: value.clone(),
                    reason,
                })?;
                Ok(Self(value))
            }

            /// Returns the identifier text.
            #[must_use]
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl std::fmt::Display for $name {
            fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str(&self.0)
            }
        }
    };
}

identifier_type!(
    ArtifactName,
    "artifact name",
    validate_artifact_name,
    "Portable ASCII artifact name without an extension."
);
identifier_type!(
    AndroidResourceName,
    "Android resource name",
    validate_android_resource_name,
    "Lowercase Android resource identifier."
);
identifier_type!(
    ApplicationId,
    "application ID",
    validate_application_id,
    "Reverse-domain freedesktop application identifier."
);
identifier_type!(
    DisplayName,
    "display name",
    validate_display_name,
    "User-facing application display name."
);
identifier_type!(
    ExecutableName,
    "executable name",
    validate_executable_name,
    "Portable executable name without arguments or path separators."
);

fn validate_artifact_name(value: &str) -> Result<(), &'static str> {
    validate_length(value, 64)?;
    if is_windows_device_name(value) {
        return Err("Windows device names are not allowed");
    }
    if !value
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '-'))
    {
        return Err("use only ASCII letters, digits, underscore, or hyphen");
    }
    if !value
        .chars()
        .next()
        .is_some_and(|character| character.is_ascii_alphanumeric())
    {
        return Err("must start with an ASCII letter or digit");
    }
    Ok(())
}

fn validate_android_resource_name(value: &str) -> Result<(), &'static str> {
    validate_length(value, 80)?;
    if is_windows_device_name(value) {
        return Err("Windows device names are not allowed");
    }
    if !value
        .chars()
        .next()
        .is_some_and(|character| character.is_ascii_lowercase())
    {
        return Err("must start with a lowercase ASCII letter");
    }
    if !value.chars().all(|character| {
        character.is_ascii_lowercase() || character.is_ascii_digit() || character == '_'
    }) {
        return Err("use only lowercase ASCII letters, digits, or underscore");
    }
    Ok(())
}

fn validate_application_id(value: &str) -> Result<(), &'static str> {
    validate_length(value, MAX_IDENTIFIER_BYTES)?;
    let mut segments = value.split('.');
    let first = segments
        .next()
        .ok_or("must contain reverse-domain segments")?;
    let second = segments
        .next()
        .ok_or("must contain at least two segments")?;
    validate_application_id_segment(first)?;
    validate_application_id_segment(second)?;
    for segment in segments {
        validate_application_id_segment(segment)?;
    }
    Ok(())
}

fn validate_application_id_segment(value: &str) -> Result<(), &'static str> {
    if !value
        .chars()
        .next()
        .is_some_and(|character| character.is_ascii_alphabetic())
    {
        return Err("each segment must start with an ASCII letter");
    }
    if !value
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '-'))
    {
        return Err("segments may contain only ASCII letters, digits, underscore, or hyphen");
    }
    Ok(())
}

fn validate_display_name(value: &str) -> Result<(), &'static str> {
    validate_length(value, 128)?;
    if value.chars().any(char::is_control) {
        return Err("control characters are not allowed");
    }
    Ok(())
}

fn validate_executable_name(value: &str) -> Result<(), &'static str> {
    validate_length(value, 128)?;
    if !value
        .chars()
        .next()
        .is_some_and(|character| character.is_ascii_alphanumeric())
    {
        return Err("must start with an ASCII letter or digit");
    }
    if !value.chars().all(|character| {
        character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '+' | '-')
    }) {
        return Err("arguments, paths, spaces, and field codes are not allowed");
    }
    Ok(())
}

fn validate_length(value: &str, maximum: usize) -> Result<(), &'static str> {
    if value.is_empty() {
        return Err("must not be empty");
    }
    if value.len() > maximum {
        return Err("value is too long");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{
        AdaptiveSources, AndroidResourceName, ApplicationId, ArtifactName, DisplayName,
        ExecutableName, IconJob, IconSources, PlatformProfile, TargetSpec,
    };
    use crate::{DomainError, RelativePath};

    fn path(value: &str) -> RelativePath {
        match RelativePath::new(value) {
            Ok(path) => path,
            Err(error) => panic!("test fixture path failed: {error}"),
        }
    }

    #[test]
    fn validates_platform_specific_identifiers() {
        assert!(ArtifactName::new("AppIcon").is_ok());
        assert!(AndroidResourceName::new("ic_launcher").is_ok());
        assert!(ApplicationId::new("com.example.Application").is_ok());
        assert!(DisplayName::new("Example 应用").is_ok());
        assert!(ExecutableName::new("example-app").is_ok());

        assert!(AndroidResourceName::new("Ic-Launcher").is_err());
        assert!(AndroidResourceName::new("nul").is_err());
        assert!(ArtifactName::new("COM1").is_err());
        assert!(ApplicationId::new("single").is_err());
        assert!(DisplayName::new("bad\n[Desktop Entry]").is_err());
        assert!(ExecutableName::new("app --unsafe").is_err());
    }

    #[test]
    fn rejects_adaptive_target_without_layers() {
        let sources = IconSources::new(path("source.png"), None);
        let resource_name = match AndroidResourceName::new("ic_launcher") {
            Ok(name) => name,
            Err(error) => panic!("test fixture resource name failed: {error}"),
        };
        let job = IconJob::new(
            path("generated"),
            sources,
            vec![TargetSpec::AndroidAdaptive { resource_name }],
        );
        assert!(job.is_err());
    }

    #[test]
    fn accepts_adaptive_target_with_layers() {
        let adaptive = AdaptiveSources::new(
            path("foreground.png"),
            path("background.png"),
            Some(path("monochrome.png")),
        );
        let sources = IconSources::new(path("source.png"), Some(adaptive));
        let resource_name = match AndroidResourceName::new("ic_launcher") {
            Ok(name) => name,
            Err(error) => panic!("test fixture resource name failed: {error}"),
        };
        let job = IconJob::new(
            path("generated"),
            sources,
            vec![TargetSpec::AndroidAdaptive { resource_name }],
        );
        assert!(job.is_ok());
    }

    #[test]
    fn rejects_duplicate_windows_msix_profiles() {
        let sources = IconSources::new(path("source.png"), None);
        let error = IconJob::new(
            path("generated"),
            sources,
            vec![TargetSpec::WindowsMsixAssets, TargetSpec::WindowsMsixAssets],
        )
        .err();

        assert_eq!(
            error,
            Some(DomainError::DuplicateTarget {
                profile: PlatformProfile::WindowsMsixAssets,
            })
        );
    }
}
