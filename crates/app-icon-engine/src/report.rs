use app_icon_domain::{ArtifactPlan, IconPlan, RelativePath};
use serde::Serialize;

/// Receipt returned only after every generated artifact has been verified and published.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct GenerationReport {
    plan: IconPlan,
    artifacts: Vec<ArtifactPlan>,
}

impl GenerationReport {
    pub(crate) fn from_plan(plan: &IconPlan) -> Self {
        Self {
            plan: plan.clone(),
            artifacts: plan.artifacts().cloned().collect(),
        }
    }

    /// Exact source and platform plan used for the published generation.
    #[must_use]
    pub const fn plan(&self) -> &IconPlan {
        &self.plan
    }

    /// Workspace-relative directory that was atomically published.
    #[must_use]
    pub const fn output_directory(&self) -> &RelativePath {
        self.plan.output_directory()
    }

    /// Verified artifacts in deterministic plan order.
    #[must_use]
    pub fn artifacts(&self) -> &[ArtifactPlan] {
        &self.artifacts
    }
}
