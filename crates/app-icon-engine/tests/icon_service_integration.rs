//! End-to-end contracts for planning and fail-closed source handling.

mod support;

use std::fs;
use std::io;

use app_icon_domain::PlatformProfile;
use app_icon_engine::IconService;
use tempfile::tempdir;

use support::{
    EXPECTED_ARTIFACTS, TestResult, all_target_job, create_sources,
    write_quadrant_png_with_dimensions,
};

#[test]
fn plan_matches_fixed_four_platform_oracle_without_writing() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let job = all_target_job("generated")?;

    let plan = IconService::new().plan(workspace.path(), &job)?;

    assert!(!workspace.path().join("generated").exists());
    assert_eq!(plan.output_directory().as_str(), "generated");
    assert_eq!(
        plan.profiles()
            .iter()
            .map(|profile| profile.profile())
            .collect::<Vec<_>>(),
        vec![
            PlatformProfile::MacOsAppIconSet,
            PlatformProfile::AndroidAdaptive,
            PlatformProfile::WindowsIco,
            PlatformProfile::LinuxXdg,
        ]
    );

    let actual_sources = plan
        .sources()
        .iter()
        .map(|source| {
            (
                source.path().as_str(),
                source.width(),
                source.height(),
                source.has_alpha(),
                source.opaque(),
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(
        actual_sources,
        vec![
            ("sources/flattened.png", 1_024, 1_024, true, true),
            ("sources/foreground.png", 1_024, 1_024, true, true),
            ("sources/background.png", 1_024, 1_024, true, true),
            ("sources/monochrome.png", 1_024, 1_024, true, true),
        ]
    );
    assert_plan_matches_oracle(&plan);
    Ok(())
}

#[test]
fn rejects_non_png_and_truncated_png_before_creating_output() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let flattened = workspace.path().join("sources/flattened.png");
    let service = IconService::new();
    let job = all_target_job("generated")?;

    fs::write(&flattened, b"this is not a PNG")?;
    let non_png = service
        .plan(workspace.path(), &job)
        .err()
        .ok_or_else(|| io::Error::other("non-PNG source unexpectedly planned"))?;
    assert_eq!(non_png.code(), "UNSUPPORTED_SOURCE_FORMAT");
    assert!(!workspace.path().join("generated").exists());

    fs::write(&flattened, b"\x89PNG\r\n\x1a\ntruncated")?;
    let truncated = service
        .plan(workspace.path(), &job)
        .err()
        .ok_or_else(|| io::Error::other("truncated PNG unexpectedly planned"))?;
    assert_eq!(truncated.code(), "SOURCE_DECODE_FAILED");
    assert!(!workspace.path().join("generated").exists());
    Ok(())
}

#[test]
fn rejects_non_square_and_undersized_flattened_sources() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let flattened = workspace.path().join("sources/flattened.png");
    let service = IconService::new();
    let job = all_target_job("generated")?;

    for (width, height) in [(1_024, 512), (512, 512)] {
        write_quadrant_png_with_dimensions(&flattened, width, height, u8::MAX)?;
        let error = service.plan(workspace.path(), &job).err().ok_or_else(|| {
            io::Error::other(format!(
                "invalid flattened source {width}x{height} unexpectedly planned"
            ))
        })?;
        assert_eq!(error.code(), "INVALID_SOURCE_DIMENSIONS");
        assert_eq!(
            error.relative_path().map(ToString::to_string).as_deref(),
            Some("sources/flattened.png")
        );
    }
    assert!(!workspace.path().join("generated").exists());
    Ok(())
}

#[test]
fn rejects_transparent_adaptive_background_before_creating_output() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    write_quadrant_png_with_dimensions(
        &workspace.path().join("sources/background.png"),
        1_024,
        1_024,
        127,
    )?;

    let error = IconService::new()
        .plan(workspace.path(), &all_target_job("generated")?)
        .err()
        .ok_or_else(|| io::Error::other("transparent adaptive background unexpectedly planned"))?;
    assert_eq!(error.code(), "ADAPTIVE_BACKGROUND_NOT_OPAQUE");
    assert_eq!(
        error.relative_path().map(ToString::to_string).as_deref(),
        Some("sources/background.png")
    );
    assert!(!workspace.path().join("generated").exists());
    Ok(())
}

#[cfg(unix)]
#[test]
fn source_symlink_is_rejected_as_non_regular_input() -> TestResult {
    use std::os::unix::fs::symlink;

    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let flattened = workspace.path().join("sources/flattened.png");
    fs::remove_file(&flattened)?;
    symlink("foreground.png", &flattened)?;

    let error = IconService::new()
        .plan(workspace.path(), &all_target_job("generated")?)
        .err()
        .ok_or_else(|| io::Error::other("source symlink unexpectedly planned"))?;
    assert_eq!(error.code(), "SOURCE_NOT_REGULAR");
    assert!(!workspace.path().join("generated").exists());
    Ok(())
}

#[test]
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
fn unsupported_atomic_publication_target_fails_without_writing() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let before = support::snapshot_files(workspace.path())?;

    let error = IconService::new()
        .generate(workspace.path(), &all_target_job("generated")?)
        .err()
        .ok_or_else(|| io::Error::other("unsupported host unexpectedly published output"))?;

    assert_eq!(error.code(), "ATOMIC_PUBLISH_UNSUPPORTED");
    assert_eq!(support::snapshot_files(workspace.path())?, before);
    assert!(!workspace.path().join("generated").exists());
    Ok(())
}

fn assert_plan_matches_oracle(plan: &app_icon_domain::IconPlan) {
    let mut actual = plan
        .artifacts()
        .map(|artifact| {
            (
                artifact.path().as_str(),
                artifact.kind(),
                artifact.pixel_width(),
                artifact.pixel_height(),
            )
        })
        .collect::<Vec<_>>();
    actual.sort_by(|left, right| left.0.cmp(right.0));

    let mut expected = EXPECTED_ARTIFACTS
        .iter()
        .map(|artifact| (artifact.path, artifact.kind, artifact.edge, artifact.edge))
        .collect::<Vec<_>>();
    expected.sort_by(|left, right| left.0.cmp(right.0));

    assert_eq!(actual, expected);
}
