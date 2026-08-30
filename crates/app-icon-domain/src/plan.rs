use std::collections::BTreeMap;

use serde::Serialize;

use crate::{DomainError, PlatformProfile, RelativePath};

/// Output artifact media/contract kind.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ArtifactKind {
    /// PNG raster image.
    Png,
    /// JSON metadata.
    Json,
    /// XML platform resource.
    Xml,
    /// Multi-frame Windows icon.
    Ico,
    /// Freedesktop desktop entry.
    DesktopEntry,
}

/// Planned output artifact.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ArtifactPlan {
    path: RelativePath,
    kind: ArtifactKind,
    pixel_width: Option<u32>,
    pixel_height: Option<u32>,
}

impl ArtifactPlan {
    /// Creates a raster artifact plan.
    #[must_use]
    pub const fn raster(
        path: RelativePath,
        kind: ArtifactKind,
        pixel_width: u32,
        pixel_height: u32,
    ) -> Self {
        Self {
            path,
            kind,
            pixel_width: Some(pixel_width),
            pixel_height: Some(pixel_height),
        }
    }

    /// Creates a non-raster artifact plan.
    #[must_use]
    pub const fn document(path: RelativePath, kind: ArtifactKind) -> Self {
        Self {
            path,
            kind,
            pixel_width: None,
            pixel_height: None,
        }
    }

    /// Artifact path relative to the job output directory.
    #[must_use]
    pub const fn path(&self) -> &RelativePath {
        &self.path
    }

    /// Artifact contract kind.
    #[must_use]
    pub const fn kind(&self) -> ArtifactKind {
        self.kind
    }

    /// Raster width when applicable.
    #[must_use]
    pub const fn pixel_width(&self) -> Option<u32> {
        self.pixel_width
    }

    /// Raster height when applicable.
    #[must_use]
    pub const fn pixel_height(&self) -> Option<u32> {
        self.pixel_height
    }
}

/// Source image facts established by the decoder.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SourceInspection {
    path: RelativePath,
    width: u32,
    height: u32,
    has_alpha: bool,
    opaque: bool,
}

impl SourceInspection {
    /// Creates decoded source facts.
    #[must_use]
    pub const fn new(
        path: RelativePath,
        width: u32,
        height: u32,
        has_alpha: bool,
        opaque: bool,
    ) -> Self {
        Self {
            path,
            width,
            height,
            has_alpha,
            opaque,
        }
    }

    /// Source path.
    #[must_use]
    pub const fn path(&self) -> &RelativePath {
        &self.path
    }

    /// Pixel width.
    #[must_use]
    pub const fn width(&self) -> u32 {
        self.width
    }

    /// Pixel height.
    #[must_use]
    pub const fn height(&self) -> u32 {
        self.height
    }

    /// Whether the decoded format carries alpha.
    #[must_use]
    pub const fn has_alpha(&self) -> bool {
        self.has_alpha
    }

    /// Whether every decoded pixel is fully opaque.
    #[must_use]
    pub const fn opaque(&self) -> bool {
        self.opaque
    }
}

/// Artifact plan for one platform profile.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ProfilePlan {
    profile: PlatformProfile,
    artifacts: Vec<ArtifactPlan>,
}

impl ProfilePlan {
    /// Creates a profile plan.
    #[must_use]
    pub const fn new(profile: PlatformProfile, artifacts: Vec<ArtifactPlan>) -> Self {
        Self { profile, artifacts }
    }

    /// Profile discriminator.
    #[must_use]
    pub const fn profile(&self) -> PlatformProfile {
        self.profile
    }

    /// Ordered artifacts for the profile.
    #[must_use]
    pub fn artifacts(&self) -> &[ArtifactPlan] {
        &self.artifacts
    }
}

/// Complete deterministic plan for an icon job.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct IconPlan {
    output_directory: RelativePath,
    sources: Vec<SourceInspection>,
    profiles: Vec<ProfilePlan>,
}

impl IconPlan {
    /// Creates a plan and rejects portable path collisions.
    pub fn new(
        output_directory: RelativePath,
        sources: Vec<SourceInspection>,
        profiles: Vec<ProfilePlan>,
    ) -> Result<Self, DomainError> {
        let mut paths: BTreeMap<String, String> = BTreeMap::new();
        for artifact in profiles.iter().flat_map(ProfilePlan::artifacts) {
            let portable_key = artifact.path().as_str().to_ascii_lowercase();
            if let Some(first) = paths.insert(portable_key, artifact.path().to_string()) {
                return Err(DomainError::ArtifactPathCollision {
                    first,
                    second: artifact.path().to_string(),
                });
            }
        }

        Ok(Self {
            output_directory,
            sources,
            profiles,
        })
    }

    /// Workspace-relative output directory.
    #[must_use]
    pub const fn output_directory(&self) -> &RelativePath {
        &self.output_directory
    }

    /// Decoded source facts.
    #[must_use]
    pub fn sources(&self) -> &[SourceInspection] {
        &self.sources
    }

    /// Ordered platform plans.
    #[must_use]
    pub fn profiles(&self) -> &[ProfilePlan] {
        &self.profiles
    }

    /// Iterates over every artifact in deterministic profile order.
    pub fn artifacts(&self) -> impl Iterator<Item = &ArtifactPlan> {
        self.profiles.iter().flat_map(ProfilePlan::artifacts)
    }
}

#[cfg(test)]
mod tests {
    use crate::{ArtifactKind, ArtifactPlan, IconPlan, PlatformProfile, ProfilePlan, RelativePath};

    fn path(value: &str) -> RelativePath {
        match RelativePath::new(value) {
            Ok(path) => path,
            Err(error) => panic!("test fixture path failed: {error}"),
        }
    }

    #[test]
    fn rejects_case_insensitive_artifact_collisions() {
        let profiles = vec![
            ProfilePlan::new(
                PlatformProfile::MacOsAppIconSet,
                vec![ArtifactPlan::document(
                    path("Mac/App.json"),
                    ArtifactKind::Json,
                )],
            ),
            ProfilePlan::new(
                PlatformProfile::LinuxXdg,
                vec![ArtifactPlan::document(
                    path("mac/app.JSON"),
                    ArtifactKind::Json,
                )],
            ),
        ];

        let plan = IconPlan::new(path("generated"), Vec::new(), profiles);
        assert!(plan.is_err());
    }
}
