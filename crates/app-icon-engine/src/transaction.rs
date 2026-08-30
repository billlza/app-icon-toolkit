//! Capability-scoped staging and atomic publication.

use std::{
    io,
    io::Write,
    path::Path,
    sync::atomic::{AtomicU64, Ordering},
};

use app_icon_domain::{ArtifactKind, IconJob, IconPlan, RelativePath};
use cap_std::fs::{Dir, OpenOptions};

use crate::{
    EngineError,
    exporters::{RenderedArtifact, render_job},
    render,
    source::PreparedSources,
};

const MAX_STAGING_ATTEMPTS: u32 = 128;
static NEXT_STAGING_ID: AtomicU64 = AtomicU64::new(0);

pub(crate) fn generate_and_publish(
    root: &Dir,
    job: &IconJob,
    plan: &IconPlan,
    sources: &PreparedSources,
) -> Result<(), EngineError> {
    ensure_atomic_publication_supported(job.output_directory())?;
    let (parent, final_name) = open_output_parent(root, job.output_directory())?;
    reject_existing_output(&parent, final_name, job.output_directory())?;

    let rendered = render_job(job, sources)?;
    validate_render_order(plan, &rendered)?;

    let staging_name = create_staging_directory(&parent, job.output_directory())?;
    let staging_result = write_and_validate_staging(&parent, &staging_name, plan, &rendered);
    if let Err(primary) = staging_result {
        return cleanup_after_error(&parent, &staging_name, job.output_directory(), primary);
    }

    match publish_no_replace(&parent, &staging_name, final_name, job.output_directory()) {
        Ok(()) => Ok(()),
        Err(primary) => {
            cleanup_after_error(&parent, &staging_name, job.output_directory(), primary)
        }
    }
}

fn open_output_parent<'a>(
    root: &Dir,
    output_directory: &'a RelativePath,
) -> Result<(Dir, &'a str), EngineError> {
    let path = output_directory.as_path();
    let final_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| EngineError::OutputParent {
            path: output_directory.clone(),
            source: io::Error::new(
                io::ErrorKind::InvalidInput,
                "output path has no UTF-8 final component",
            ),
        })?;
    let parent_path = match path.parent() {
        Some(parent) => parent,
        None => Path::new(""),
    };
    let parent = if parent_path.as_os_str().is_empty() {
        root.try_clone()
    } else {
        root.open_dir(parent_path)
    }
    .map_err(|source| EngineError::OutputParent {
        path: output_directory.clone(),
        source,
    })?;
    Ok((parent, final_name))
}

fn reject_existing_output(
    parent: &Dir,
    final_name: &str,
    output_directory: &RelativePath,
) -> Result<(), EngineError> {
    match parent.symlink_metadata(final_name) {
        Ok(_) => Err(EngineError::OutputExists {
            path: output_directory.clone(),
        }),
        Err(source) if source.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(source) => Err(EngineError::OutputParent {
            path: output_directory.clone(),
            source,
        }),
    }
}

fn create_staging_directory(
    parent: &Dir,
    output_directory: &RelativePath,
) -> Result<String, EngineError> {
    let process_id = std::process::id();
    let staging_id = NEXT_STAGING_ID.fetch_add(1, Ordering::Relaxed);
    for attempt in 0..MAX_STAGING_ATTEMPTS {
        let name = format!(".app-icon-toolkit-staging-{process_id}-{staging_id}-{attempt}");
        match parent.create_dir(&name) {
            Ok(()) => return Ok(name),
            Err(source) if source.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(source) => {
                return Err(EngineError::StagingCreate {
                    path: output_directory.clone(),
                    source,
                });
            }
        }
    }
    Err(EngineError::StagingCreate {
        path: output_directory.clone(),
        source: io::Error::new(
            io::ErrorKind::AlreadyExists,
            "all bounded sibling staging names are occupied",
        ),
    })
}

fn write_and_validate_staging(
    parent: &Dir,
    staging_name: &str,
    plan: &IconPlan,
    rendered: &[RenderedArtifact],
) -> Result<(), EngineError> {
    let staging = parent
        .open_dir(staging_name)
        .map_err(|source| EngineError::StagingCreate {
            path: plan.output_directory().clone(),
            source,
        })?;

    for artifact in rendered {
        write_artifact(&staging, artifact)?;
    }
    for (artifact_plan, rendered_artifact) in plan.artifacts().zip(rendered) {
        validate_written_artifact(&staging, artifact_plan, rendered_artifact)?;
    }
    Ok(())
}

fn write_artifact(staging: &Dir, artifact: &RenderedArtifact) -> Result<(), EngineError> {
    if let Some(parent) = artifact.path.as_path().parent()
        && !parent.as_os_str().is_empty()
    {
        staging
            .create_dir_all(parent)
            .map_err(|source| EngineError::ArtifactParent {
                path: artifact.path.clone(),
                source,
            })?;
    }

    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    let mut file = staging
        .open_with(artifact.path.as_path(), &options)
        .map_err(|source| EngineError::ArtifactCreate {
            path: artifact.path.clone(),
            source,
        })?;
    file.write_all(&artifact.bytes)
        .map_err(|source| EngineError::ArtifactWrite {
            path: artifact.path.clone(),
            source,
        })?;
    file.sync_all()
        .map_err(|source| EngineError::ArtifactWrite {
            path: artifact.path.clone(),
            source,
        })?;
    Ok(())
}

fn validate_written_artifact(
    staging: &Dir,
    plan: &app_icon_domain::ArtifactPlan,
    rendered: &RenderedArtifact,
) -> Result<(), EngineError> {
    let bytes =
        staging
            .read(plan.path().as_path())
            .map_err(|source| EngineError::ArtifactRead {
                path: plan.path().clone(),
                source,
            })?;
    if bytes != rendered.bytes {
        return Err(EngineError::ArtifactValidation {
            path: plan.path().clone(),
            reason: "read-back bytes differ from the encoded artifact".to_owned(),
        });
    }

    let validation = match plan.kind() {
        ArtifactKind::Png => match (plan.pixel_width(), plan.pixel_height()) {
            (Some(width), Some(height)) if width == height => render::validate_png(&bytes, width),
            _ => Err("PNG plan does not contain equal non-null dimensions".to_owned()),
        },
        ArtifactKind::Json => serde_json::from_slice::<serde_json::Value>(&bytes)
            .map(|_| ())
            .map_err(|error| format!("JSON decode failed: {error}")),
        ArtifactKind::Xml => validate_xml(&bytes),
        ArtifactKind::Ico => render::validate_ico(&bytes, &crate::exporters::WINDOWS_FRAME_SIZES),
        ArtifactKind::DesktopEntry => validate_desktop_entry(&bytes),
    };
    validation.map_err(|reason| EngineError::ArtifactValidation {
        path: plan.path().clone(),
        reason,
    })
}

fn validate_xml(bytes: &[u8]) -> Result<(), String> {
    let text = std::str::from_utf8(bytes).map_err(|error| format!("XML is not UTF-8: {error}"))?;
    if !text.starts_with("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n") {
        return Err("XML declaration is missing or unexpected".to_owned());
    }
    if !text.contains("<adaptive-icon ") || !text.ends_with("</adaptive-icon>\n") {
        return Err("adaptive-icon root element is missing or incomplete".to_owned());
    }
    Ok(())
}

fn validate_desktop_entry(bytes: &[u8]) -> Result<(), String> {
    let text = std::str::from_utf8(bytes)
        .map_err(|error| format!("desktop entry is not UTF-8: {error}"))?;
    if !text.starts_with("[Desktop Entry]\n") {
        return Err("desktop entry group header is missing".to_owned());
    }
    for required in [
        "Type=Application\n",
        "Name=",
        "Exec=",
        "Icon=",
        "Terminal=false\n",
    ] {
        if !text.contains(required) {
            return Err(format!(
                "desktop entry is missing `{}`",
                required.trim_end()
            ));
        }
    }
    Ok(())
}

fn validate_render_order(
    plan: &IconPlan,
    rendered: &[RenderedArtifact],
) -> Result<(), EngineError> {
    let planned_count = plan.artifacts().count();
    if planned_count != rendered.len() {
        return Err(EngineError::ArtifactValidation {
            path: plan.output_directory().clone(),
            reason: format!(
                "renderer produced {} artifacts for a {planned_count}-artifact plan",
                rendered.len()
            ),
        });
    }
    for (planned, actual) in plan.artifacts().zip(rendered) {
        if planned.path() != &actual.path {
            return Err(EngineError::ArtifactValidation {
                path: planned.path().clone(),
                reason: format!("renderer returned unexpected path `{}`", actual.path),
            });
        }
    }
    Ok(())
}

fn cleanup_after_error(
    parent: &Dir,
    staging_name: &str,
    output_directory: &RelativePath,
    primary: EngineError,
) -> Result<(), EngineError> {
    match parent.remove_dir_all(staging_name) {
        Ok(()) => Err(primary),
        Err(source) => Err(EngineError::StagingCleanup {
            path: output_directory.clone(),
            primary: primary.to_string(),
            source,
        }),
    }
}

#[cfg(any(
    target_os = "android",
    target_os = "ios",
    target_os = "linux",
    target_os = "macos",
    target_os = "redox",
    target_os = "tvos",
    target_os = "visionos",
    target_os = "watchos",
    windows
))]
fn ensure_atomic_publication_supported(
    _output_directory: &RelativePath,
) -> Result<(), EngineError> {
    Ok(())
}

#[cfg(not(any(
    target_os = "android",
    target_os = "ios",
    target_os = "linux",
    target_os = "macos",
    target_os = "redox",
    target_os = "tvos",
    target_os = "visionos",
    target_os = "watchos",
    windows
)))]
fn ensure_atomic_publication_supported(output_directory: &RelativePath) -> Result<(), EngineError> {
    Err(EngineError::AtomicPublishUnsupported {
        path: output_directory.clone(),
        reason: "no audited atomic no-replace primitive is available for this target".to_owned(),
    })
}

#[cfg(any(
    target_os = "android",
    target_os = "ios",
    target_os = "linux",
    target_os = "macos",
    target_os = "redox",
    target_os = "tvos",
    target_os = "visionos",
    target_os = "watchos"
))]
fn publish_no_replace(
    parent: &Dir,
    staging_name: &str,
    final_name: &str,
    output_directory: &RelativePath,
) -> Result<(), EngineError> {
    use rustix::fs::{RenameFlags, renameat_with};

    match renameat_with(
        parent,
        staging_name,
        parent,
        final_name,
        RenameFlags::NOREPLACE,
    ) {
        Ok(()) => Ok(()),
        Err(source) if source == rustix::io::Errno::EXIST => Err(EngineError::OutputExists {
            path: output_directory.clone(),
        }),
        Err(source)
            if source == rustix::io::Errno::NOSYS
                || source == rustix::io::Errno::NOTSUP
                || source == rustix::io::Errno::INVAL =>
        {
            Err(EngineError::AtomicPublishUnsupported {
                path: output_directory.clone(),
                reason: "the filesystem rejected renameat_with(NOREPLACE)".to_owned(),
            })
        }
        Err(source) => Err(EngineError::Publish {
            path: output_directory.clone(),
            source: source.into(),
        }),
    }
}

#[cfg(windows)]
fn publish_no_replace(
    parent: &Dir,
    staging_name: &str,
    final_name: &str,
    output_directory: &RelativePath,
) -> Result<(), EngineError> {
    match app_icon_windows_fs::rename_directory_no_replace(parent, staging_name, final_name) {
        Ok(()) => Ok(()),
        Err(app_icon_windows_fs::RenameError::AlreadyExists) => Err(EngineError::OutputExists {
            path: output_directory.clone(),
        }),
        Err(app_icon_windows_fs::RenameError::Unsupported(source)) => {
            Err(EngineError::AtomicPublishUnsupported {
                path: output_directory.clone(),
                reason: format!("Windows handle-relative rename is unsupported: {source}"),
            })
        }
        Err(app_icon_windows_fs::RenameError::Other(source)) => Err(EngineError::Publish {
            path: output_directory.clone(),
            source,
        }),
    }
}

#[cfg(not(any(
    target_os = "android",
    target_os = "ios",
    target_os = "linux",
    target_os = "macos",
    target_os = "redox",
    target_os = "tvos",
    target_os = "visionos",
    target_os = "watchos",
    windows
)))]
fn publish_no_replace(
    _parent: &Dir,
    _staging_name: &str,
    _final_name: &str,
    output_directory: &RelativePath,
) -> Result<(), EngineError> {
    Err(EngineError::AtomicPublishUnsupported {
        path: output_directory.clone(),
        reason: "no audited atomic no-replace primitive is available for this target".to_owned(),
    })
}
