//! End-to-end contracts for planning and fail-closed source handling.

mod support;

use std::fs;
use std::io;

use app_icon_domain::{ArtifactKind, PlatformProfile};
use app_icon_engine::IconService;
use image::ImageReader;
use tempfile::tempdir;

use support::{
    EXPECTED_ARTIFACTS, TestResult, all_target_job, create_sources, snapshot_files,
    windows_msix_job, write_quadrant_png_with_dimensions,
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
fn windows_msix_profile_generates_the_exact_57_asset_matrix() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let job = windows_msix_job("generated")?;
    let service = IconService::new();
    let expected = expected_windows_msix_matrix();

    let plan = service.plan(workspace.path(), &job)?;
    assert_eq!(plan.profiles().len(), 1);
    assert_eq!(
        plan.profiles()[0].profile(),
        PlatformProfile::WindowsMsixAssets
    );
    let mut actual = plan
        .artifacts()
        .map(|artifact| {
            (
                artifact.path().to_string(),
                artifact.kind(),
                artifact.pixel_width(),
                artifact.pixel_height(),
            )
        })
        .collect::<Vec<_>>();
    actual.sort_by(|left, right| left.0.cmp(&right.0));
    let expected_plan = expected
        .iter()
        .map(|(path, pixels)| {
            (
                path.clone(),
                ArtifactKind::Png,
                Some(*pixels),
                Some(*pixels),
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(actual, expected_plan);

    let report = service.generate(workspace.path(), &job)?;
    assert_eq!(report.artifacts().len(), 57);
    let output = workspace.path().join("generated");
    let snapshot = snapshot_files(&output)?;
    assert_eq!(snapshot.len(), 57);
    assert_eq!(
        snapshot.keys().cloned().collect::<Vec<_>>(),
        expected
            .iter()
            .map(|(path, _)| path.clone())
            .collect::<Vec<_>>()
    );

    let smallest = ImageReader::open(output.join("windows/msix/Assets/AppList.targetsize-16.png"))?
        .decode()?;
    let largest =
        ImageReader::open(output.join("windows/msix/Assets/MedTile.scale-400.png"))?.decode()?;
    assert_eq!(smallest.width(), 16);
    assert_eq!(smallest.height(), 16);
    assert_eq!(largest.width(), 600);
    assert_eq!(largest.height(), 600);
    assert_eq!(
        snapshot.get("windows/msix/Assets/AppList.targetsize-16.png"),
        snapshot.get("windows/msix/Assets/AppList.targetsize-16_altform-unplated.png")
    );
    assert_eq!(
        snapshot.get("windows/msix/Assets/AppList.targetsize-16.png"),
        snapshot.get("windows/msix/Assets/AppList.targetsize-16_altform-lightunplated.png")
    );
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

fn expected_windows_msix_matrix() -> Vec<(String, u32)> {
    const TARGET_SIZES: [u32; 14] = [16, 20, 24, 30, 32, 36, 40, 48, 60, 64, 72, 80, 96, 256];
    const TARGET_SUFFIXES: [&str; 3] = ["", "_altform-unplated", "_altform-lightunplated"];
    const SCALE_QUALIFIERS: [u16; 5] = [100, 125, 150, 200, 400];
    const SCALED_ASSETS: [(&str, [u32; 5]); 3] = [
        ("AppList", [44, 55, 66, 88, 176]),
        ("MedTile", [150, 188, 225, 300, 600]),
        ("StoreLogo", [50, 63, 75, 100, 200]),
    ];

    let mut expected = Vec::with_capacity(57);
    for size in TARGET_SIZES {
        for suffix in TARGET_SUFFIXES {
            expected.push((
                format!("windows/msix/Assets/AppList.targetsize-{size}{suffix}.png"),
                size,
            ));
        }
    }
    for (stem, pixels) in SCALED_ASSETS {
        for (scale, pixels) in SCALE_QUALIFIERS.into_iter().zip(pixels) {
            expected.push((
                format!("windows/msix/Assets/{stem}.scale-{scale}.png"),
                pixels,
            ));
        }
    }
    expected.sort_by(|left, right| left.0.cmp(&right.0));
    expected
}
