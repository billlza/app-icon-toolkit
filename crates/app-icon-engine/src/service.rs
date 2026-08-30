use std::path::Path;

use app_icon_domain::{IconJob, IconPlan};
use cap_std::{ambient_authority, fs::Dir};

use crate::{EngineError, GenerationReport};

/// Stateless entry point for deterministic icon planning and generation.
#[derive(Debug, Clone, Copy, Default)]
pub struct IconService;

impl IconService {
    /// Creates an icon service with the fixed first-release profiles.
    #[must_use]
    pub const fn new() -> Self {
        Self
    }

    /// Inspects all inputs and builds the exact artifact plan without writing files.
    pub fn plan(&self, workspace_root: &Path, job: &IconJob) -> Result<IconPlan, EngineError> {
        let root = open_workspace_root(workspace_root)?;
        self.plan_in(&root, job)
    }

    /// Generates, verifies, and atomically publishes a new output directory.
    pub fn generate(
        &self,
        workspace_root: &Path,
        job: &IconJob,
    ) -> Result<GenerationReport, EngineError> {
        let root = open_workspace_root(workspace_root)?;
        let (plan, sources) = crate::exporters::prepare_job(&root, job)?;
        crate::transaction::generate_and_publish(&root, job, &plan, &sources)?;
        Ok(GenerationReport::from_plan(&plan))
    }

    fn plan_in(&self, root: &Dir, job: &IconJob) -> Result<IconPlan, EngineError> {
        crate::exporters::build_plan(root, job)
    }
}

fn open_workspace_root(path: &Path) -> Result<Dir, EngineError> {
    Dir::open_ambient_dir(path, ambient_authority()).map_err(|source| EngineError::WorkspaceRoot {
        path: path.to_path_buf(),
        source,
    })
}
