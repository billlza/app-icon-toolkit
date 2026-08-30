//! Capability-scoped staging and atomic publication.

mod pin;
mod reconcile;

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
use pin::{NameObservation, StagingPin};
use reconcile::{EntryState, NativeResult, Resolution, resolve};

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
    let staging_path = staging_relative_path(job.output_directory(), &staging_name)?;
    let staging = StagingPin::open(&parent, &staging_name).map_err(|source| {
        EngineError::StagingIdentity {
            path: job.output_directory().clone(),
            staging_path: staging_path.clone(),
            source,
        }
    })?;
    let staging_result = write_and_validate_staging(staging.directory(), plan, &rendered);
    if let Err(primary) = staging_result {
        return preserve_staging(&staging_path, job.output_directory(), primary);
    }

    let native_result = publish_no_replace(
        &parent,
        &staging_name,
        final_name,
        &staging,
        job.output_directory(),
    );
    reconcile_publication(
        &parent,
        &staging_name,
        final_name,
        &staging,
        &staging_path,
        job.output_directory(),
        native_result,
    )
}

fn reconcile_publication(
    parent: &Dir,
    staging_name: &str,
    final_name: &str,
    staging: &StagingPin,
    staging_path: &RelativePath,
    output_directory: &RelativePath,
    native_result: Result<(), EngineError>,
) -> Result<(), EngineError> {
    let native_state = if native_result.is_ok() {
        NativeResult::Succeeded
    } else {
        NativeResult::Failed
    };
    let native_description = match &native_result {
        Ok(()) => "native no-replace rename reported success".to_owned(),
        Err(error) => error.to_string(),
    };
    let primary_code = native_result.as_ref().err().map(EngineError::code);
    let staging_state = observe_entry(parent, staging_name, staging);
    let final_state = observe_entry(parent, final_name, staging);

    match resolve(native_state, &staging_state, &final_state) {
        Resolution::Published => Ok(()),
        Resolution::NotPublished => match native_result {
            Err(primary) => preserve_staging(staging_path, output_directory, primary),
            Ok(()) => Err(EngineError::PublishOutcomeIndeterminate {
                path: output_directory.clone(),
                staging_path: staging_path.clone(),
                native_result: native_description,
                primary_code,
                reconciliation_reason:
                    "state classifier returned not-published after native success".to_owned(),
            }),
        },
        Resolution::Indeterminate(reconciliation_reason) => {
            Err(EngineError::PublishOutcomeIndeterminate {
                path: output_directory.clone(),
                staging_path: staging_path.clone(),
                native_result: native_description,
                primary_code,
                reconciliation_reason,
            })
        }
    }
}

fn staging_relative_path(
    output_directory: &RelativePath,
    staging_name: &str,
) -> Result<RelativePath, EngineError> {
    let path = match output_directory.as_str().rsplit_once('/') {
        Some((parent, _)) => format!("{parent}/{staging_name}"),
        None => staging_name.to_owned(),
    };
    RelativePath::new(path).map_err(EngineError::from)
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
    staging: &Dir,
    plan: &IconPlan,
    rendered: &[RenderedArtifact],
) -> Result<(), EngineError> {
    for artifact in rendered {
        write_artifact(staging, artifact)?;
    }
    for (artifact_plan, rendered_artifact) in plan.artifacts().zip(rendered) {
        validate_written_artifact(staging, artifact_plan, rendered_artifact)?;
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

fn preserve_staging(
    staging_path: &RelativePath,
    output_directory: &RelativePath,
    primary: EngineError,
) -> Result<(), EngineError> {
    Err(EngineError::StagingPreserved {
        path: output_directory.clone(),
        staging_path: staging_path.clone(),
        primary: Box::new(primary),
    })
}

fn observe_entry(parent: &Dir, name: &str, staging: &StagingPin) -> EntryState {
    match staging.observe_name(parent, name) {
        Ok(NameObservation::Missing) => EntryState::Missing,
        Ok(NameObservation::Same) => EntryState::Expected,
        Ok(NameObservation::Different) => EntryState::Different,
        Err(source) => EntryState::Unobservable(source.to_string()),
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
    staging: &StagingPin,
    output_directory: &RelativePath,
) -> Result<(), EngineError> {
    use rustix::fs::{RenameFlags, renameat_with};

    match staging.observe_name(parent, staging_name) {
        Ok(NameObservation::Same) => {}
        Ok(NameObservation::Missing | NameObservation::Different) => {
            return Err(EngineError::Publish {
                path: output_directory.clone(),
                source: io::Error::other(
                    "staging directory identity changed immediately before publication",
                ),
            });
        }
        Err(source) => {
            return Err(EngineError::Publish {
                path: output_directory.clone(),
                source,
            });
        }
    }

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
    _staging_name: &str,
    final_name: &str,
    staging: &StagingPin,
    output_directory: &RelativePath,
) -> Result<(), EngineError> {
    match app_icon_windows_fs::rename_directory_no_replace(
        parent,
        final_name,
        staging.windows_pin(),
    ) {
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
    _staging: &StagingPin,
    output_directory: &RelativePath,
) -> Result<(), EngineError> {
    Err(EngineError::AtomicPublishUnsupported {
        path: output_directory.clone(),
        reason: "no audited atomic no-replace primitive is available for this target".to_owned(),
    })
}

#[cfg(test)]
mod tests {
    use std::{fs, io};

    #[cfg(unix)]
    use cap_std::{ambient_authority, fs::Dir};
    use tempfile::tempdir;

    #[cfg(unix)]
    use super::pin::StagingPin;
    use super::{preserve_staging, staging_relative_path};
    #[cfg(unix)]
    use super::{publish_no_replace, reconcile_publication};
    use crate::{EngineError, PublicationState, RetryAdvice};
    use app_icon_domain::RelativePath;

    type TestResult<T = ()> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

    fn validation_failure() -> Result<(), EngineError> {
        Err(EngineError::ArtifactValidation {
            path: RelativePath::new("artifact.png")?,
            reason: "injected validation failure".to_owned(),
        })
    }

    #[test]
    fn failed_generation_preserves_staging_and_typed_primary_context() -> TestResult {
        let temporary = tempdir()?;
        let staging = temporary.path().join("staging");
        fs::create_dir(&staging)?;
        fs::write(staging.join("sentinel"), b"preserved")?;
        let output = RelativePath::new("published")?;
        let staging_path = RelativePath::new("staging")?;
        let primary = validation_failure()
            .err()
            .ok_or_else(|| io::Error::other("test failure fixture unexpectedly succeeded"))?;

        let error = preserve_staging(&staging_path, &output, primary)
            .err()
            .ok_or_else(|| io::Error::other("preservation unexpectedly reported success"))?;

        assert_eq!(error.code(), "ARTIFACT_VALIDATION_FAILED");
        assert_eq!(error.primary_code(), Some("ARTIFACT_VALIDATION_FAILED"));
        assert_eq!(
            error.publication_state(),
            Some(PublicationState::NotPublished)
        );
        assert_eq!(error.retry_advice(), Some(RetryAdvice::MayRetry));
        assert_eq!(
            error.staging_relative_path().map(ToString::to_string),
            Some("staging".to_owned())
        );
        assert_eq!(fs::read(staging.join("sentinel"))?, b"preserved");
        Ok(())
    }

    #[test]
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
    fn publication_rejects_a_replaced_staging_identity() -> TestResult {
        let temporary = tempdir()?;
        let staging = temporary.path().join("staging");
        fs::create_dir(&staging)?;
        let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;
        let pinned = StagingPin::open(&parent, "staging")?;
        fs::remove_dir(&staging)?;
        fs::create_dir(&staging)?;
        fs::write(staging.join("replacement"), b"not validated")?;
        assert!(!pinned.matches_name(&parent, "staging")?);
        let output = RelativePath::new("published")?;

        let error = publish_no_replace(&parent, "staging", "published", &pinned, &output)
            .err()
            .ok_or_else(|| io::Error::other("replacement staging was unexpectedly published"))?;

        assert_eq!(error.code(), "ATOMIC_PUBLISH_FAILED");
        assert!(staging.join("replacement").is_file());
        assert!(!temporary.path().join("published").exists());
        Ok(())
    }

    #[test]
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
    fn native_success_cannot_hide_source_replacement_in_the_final_window() -> TestResult {
        let temporary = tempdir()?;
        let staging = temporary.path().join("staging");
        let displaced = temporary.path().join("displaced");
        fs::create_dir(&staging)?;
        fs::write(staging.join("original"), b"validated")?;
        let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;
        let pinned = StagingPin::open(&parent, "staging")?;
        assert!(pinned.matches_name(&parent, "staging")?);

        fs::rename(&staging, &displaced)?;
        fs::create_dir(&staging)?;
        fs::write(staging.join("replacement"), b"not validated")?;
        fs::rename(&staging, temporary.path().join("published"))?;

        let staging_path = RelativePath::new("staging")?;
        let output = RelativePath::new("published")?;
        let error = reconcile_publication(
            &parent,
            "staging",
            "published",
            &pinned,
            &staging_path,
            &output,
            Ok(()),
        )
        .err()
        .ok_or_else(|| io::Error::other("replacement publication was unexpectedly accepted"))?;

        assert_eq!(error.code(), "ATOMIC_PUBLISH_INDETERMINATE");
        assert_eq!(
            error.publication_state(),
            Some(PublicationState::Indeterminate)
        );
        assert_eq!(error.retry_advice(), Some(RetryAdvice::ReconcileFirst));
        assert_eq!(fs::read(displaced.join("original"))?, b"validated");
        assert_eq!(
            fs::read(temporary.path().join("published/replacement"))?,
            b"not validated"
        );
        Ok(())
    }

    #[test]
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
    fn native_error_after_real_commit_is_reconciled_as_success() -> TestResult {
        let temporary = tempdir()?;
        let staging = temporary.path().join("staging");
        fs::create_dir(&staging)?;
        fs::write(staging.join("original"), b"validated")?;
        let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;
        let pinned = StagingPin::open(&parent, "staging")?;
        fs::rename(&staging, temporary.path().join("published"))?;
        let staging_path = RelativePath::new("staging")?;
        let output = RelativePath::new("published")?;
        let late_error = EngineError::Publish {
            path: output.clone(),
            source: io::Error::other("injected late native error"),
        };

        reconcile_publication(
            &parent,
            "staging",
            "published",
            &pinned,
            &staging_path,
            &output,
            Err(late_error),
        )?;
        assert_eq!(
            fs::read(temporary.path().join("published/original"))?,
            b"validated"
        );
        Ok(())
    }

    #[test]
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
    fn native_error_before_commit_preserves_staging_with_primary_context() -> TestResult {
        let temporary = tempdir()?;
        let staging = temporary.path().join("staging");
        fs::create_dir(&staging)?;
        fs::write(staging.join("original"), b"validated")?;
        let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;
        let pinned = StagingPin::open(&parent, "staging")?;
        let staging_path = RelativePath::new("staging")?;
        let output = RelativePath::new("published")?;
        let native_error = EngineError::Publish {
            path: output.clone(),
            source: io::Error::other("injected native failure before commit"),
        };

        let error = reconcile_publication(
            &parent,
            "staging",
            "published",
            &pinned,
            &staging_path,
            &output,
            Err(native_error),
        )
        .err()
        .ok_or_else(|| io::Error::other("failed publication unexpectedly reported success"))?;

        assert_eq!(error.code(), "ATOMIC_PUBLISH_FAILED");
        assert_eq!(error.primary_code(), Some("ATOMIC_PUBLISH_FAILED"));
        assert_eq!(
            error.publication_state(),
            Some(PublicationState::NotPublished)
        );
        assert_eq!(error.retry_advice(), Some(RetryAdvice::MayRetry));
        assert_eq!(
            error.staging_relative_path().map(ToString::to_string),
            Some("staging".to_owned())
        );
        assert_eq!(fs::read(staging.join("original"))?, b"validated");
        assert!(!temporary.path().join("published").exists());
        Ok(())
    }

    #[test]
    fn staging_path_is_a_sibling_of_the_final_component() -> TestResult {
        let nested = RelativePath::new("build/icons")?;
        assert_eq!(
            staging_relative_path(&nested, ".app-icon-toolkit-staging-1-2-3")?.as_str(),
            "build/.app-icon-toolkit-staging-1-2-3"
        );
        let top_level = RelativePath::new("icons")?;
        assert_eq!(
            staging_relative_path(&top_level, ".app-icon-toolkit-staging-1-2-3")?.as_str(),
            ".app-icon-toolkit-staging-1-2-3"
        );
        Ok(())
    }
}
