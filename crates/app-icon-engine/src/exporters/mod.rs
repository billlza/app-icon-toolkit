mod android;
mod linux;
mod macos;
mod windows;

pub(crate) use windows::FRAME_SIZES as WINDOWS_FRAME_SIZES;

use app_icon_domain::{IconJob, IconPlan, ProfilePlan, RelativePath};
use cap_std::fs::Dir;

use crate::{
    EngineError,
    render::PngCache,
    source::{PreparedSources, inspect_sources, prepare_sources},
};

pub(crate) struct RenderedArtifact {
    pub(crate) path: RelativePath,
    pub(crate) bytes: Vec<u8>,
}

impl RenderedArtifact {
    pub(crate) const fn new(path: RelativePath, bytes: Vec<u8>) -> Self {
        Self { path, bytes }
    }
}

pub(crate) fn build_plan(root: &Dir, job: &IconJob) -> Result<IconPlan, EngineError> {
    build_plan_for_sources(job, inspect_sources(root, job.sources())?)
}

pub(crate) fn prepare_job(
    root: &Dir,
    job: &IconJob,
) -> Result<(IconPlan, PreparedSources), EngineError> {
    let sources = prepare_sources(root, job.sources())?;
    let plan = build_plan_for_sources(job, sources.inspections())?;
    Ok((plan, sources))
}

fn build_plan_for_sources(
    job: &IconJob,
    sources: Vec<app_icon_domain::SourceInspection>,
) -> Result<IconPlan, EngineError> {
    let mut profiles = Vec::with_capacity(job.targets().len());

    for target in job.targets() {
        let profile = match target {
            app_icon_domain::TargetSpec::MacOsAppIconSet { icon_set_name } => {
                macos::plan(icon_set_name)?
            }
            app_icon_domain::TargetSpec::AndroidAdaptive { resource_name } => {
                android::plan(resource_name, job.sources().adaptive())?
            }
            app_icon_domain::TargetSpec::WindowsIco { file_stem } => windows::plan(file_stem)?,
            app_icon_domain::TargetSpec::LinuxXdg {
                application_id,
                display_name: _,
                executable: _,
            } => linux::plan(application_id)?,
        };
        profiles.push(profile);
    }

    IconPlan::new(job.output_directory().clone(), sources, profiles).map_err(EngineError::from)
}

pub(crate) fn profile(
    profile: app_icon_domain::PlatformProfile,
    artifacts: Vec<app_icon_domain::ArtifactPlan>,
) -> ProfilePlan {
    ProfilePlan::new(profile, artifacts)
}

pub(crate) fn render_job(
    job: &IconJob,
    sources: &PreparedSources,
) -> Result<Vec<RenderedArtifact>, EngineError> {
    let flattened = &sources.flattened.image;
    let mut artifacts = Vec::new();
    let mut cache = PngCache::default();

    for target in job.targets() {
        let mut rendered = match target {
            app_icon_domain::TargetSpec::MacOsAppIconSet { icon_set_name } => {
                macos::render(icon_set_name, flattened, &mut cache)?
            }
            app_icon_domain::TargetSpec::AndroidAdaptive { resource_name } => android::render(
                resource_name,
                sources.adaptive.as_ref(),
                flattened,
                &mut cache,
            )?,
            app_icon_domain::TargetSpec::WindowsIco { file_stem } => {
                windows::render(file_stem, flattened, &mut cache)?
            }
            app_icon_domain::TargetSpec::LinuxXdg {
                application_id,
                display_name,
                executable,
            } => linux::render(
                application_id,
                display_name,
                executable,
                flattened,
                &mut cache,
            )?,
        };
        artifacts.append(&mut rendered);
    }

    Ok(artifacts)
}
