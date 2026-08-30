use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{self, BufReader};
use std::path::Path;

use app_icon_domain::ArtifactKind;
use app_icon_engine::IconService;
use ico::IconDir;
use image::{ImageBuffer, ImageReader, Rgba, RgbaImage};
use serde_json::Value;
use tempfile::tempdir;

use super::{
    BLUE, EXPECTED_ARTIFACTS, GREEN, RED, TestResult, YELLOW, all_target_job, create_sources,
    snapshot_files, target_job,
};

#[test]
fn generate_writes_decodable_and_internally_consistent_artifacts() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let job = all_target_job("generated")?;
    let service = IconService::new();
    let planned = service.plan(workspace.path(), &job)?;

    let report = service.generate(workspace.path(), &job)?;
    let output = workspace.path().join("generated");

    assert_eq!(report.plan(), &planned);
    assert_eq!(report.output_directory().as_str(), "generated");
    assert_eq!(
        report.artifacts(),
        planned.artifacts().cloned().collect::<Vec<_>>().as_slice()
    );

    let mut expected_paths = expected_paths();
    expected_paths.sort_unstable();
    assert_eq!(relative_file_paths(&output)?, expected_paths);

    for expected in EXPECTED_ARTIFACTS {
        if expected.kind == ArtifactKind::Png {
            let edge = expected
                .edge
                .ok_or_else(|| io::Error::other("PNG oracle omitted its pixel edge"))?;
            assert_quadrant_png(&output.join(expected.path), edge)?;
        }
    }

    assert_macos_contents_json(&output)?;
    assert_android_adaptive_xml(&output)?;
    assert_windows_ico(&output)?;
    assert_linux_desktop_entry(&output)?;
    Ok(())
}

#[test]
fn existing_output_is_preserved_and_generation_leaves_no_staging_files() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let output = workspace.path().join("generated");
    fs::create_dir(&output)?;
    fs::write(output.join("sentinel.txt"), b"do not replace")?;
    let before = snapshot_files(workspace.path())?;

    let result = IconService::new().generate(workspace.path(), &all_target_job("generated")?);

    let error = result
        .err()
        .ok_or_else(|| io::Error::other("generation unexpectedly replaced an existing output"))?;
    assert_eq!(error.code(), "OUTPUT_EXISTS");
    assert_eq!(snapshot_files(workspace.path())?, before);
    Ok(())
}

#[test]
fn existing_file_at_output_path_is_preserved() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let output = workspace.path().join("generated");
    fs::write(&output, b"do not replace")?;
    let before = snapshot_files(workspace.path())?;

    let result = IconService::new().generate(workspace.path(), &all_target_job("generated")?);

    let error = result
        .err()
        .ok_or_else(|| io::Error::other("generation unexpectedly replaced an existing file"))?;
    assert_eq!(error.code(), "OUTPUT_EXISTS");
    assert_eq!(snapshot_files(workspace.path())?, before);
    assert_eq!(fs::read(output)?, b"do not replace");
    Ok(())
}

#[test]
fn missing_output_parent_fails_without_creating_any_path() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let before = snapshot_files(workspace.path())?;

    let result =
        IconService::new().generate(workspace.path(), &all_target_job("missing/generated")?);

    let error = result
        .err()
        .ok_or_else(|| io::Error::other("generation unexpectedly created a missing parent"))?;
    assert_eq!(error.code(), "OUTPUT_PARENT_UNAVAILABLE");
    assert_eq!(snapshot_files(workspace.path())?, before);
    assert!(!workspace.path().join("missing").exists());
    Ok(())
}

#[test]
fn repeated_generation_to_distinct_destinations_is_byte_deterministic() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let service = IconService::new();

    service.generate(workspace.path(), &all_target_job("first")?)?;
    service.generate(workspace.path(), &all_target_job("second")?)?;

    assert_eq!(
        snapshot_files(&workspace.path().join("first"))?,
        snapshot_files(&workspace.path().join("second"))?
    );
    Ok(())
}

#[test]
fn concurrent_generation_to_same_output_has_one_atomic_winner() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let workspace_path = workspace.path().to_path_buf();
    let job = all_target_job("generated")?;
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(3));
    let mut workers = Vec::new();

    for _ in 0..2 {
        let worker_barrier = std::sync::Arc::clone(&barrier);
        let worker_root = workspace_path.clone();
        let worker_job = job.clone();
        workers.push(std::thread::spawn(move || {
            worker_barrier.wait();
            IconService::new()
                .generate(&worker_root, &worker_job)
                .map(|_| ())
                .map_err(|error| error.code().to_owned())
        }));
    }
    barrier.wait();

    let mut successes = 0;
    let mut failures = Vec::new();
    for worker in workers {
        match worker
            .join()
            .map_err(|_| io::Error::other("generation worker panicked"))?
        {
            Ok(()) => successes += 1,
            Err(code) => failures.push(code),
        }
    }
    assert_eq!(successes, 1);
    assert_eq!(failures, vec!["OUTPUT_EXISTS"]);

    let output = workspace.path().join("generated");
    let mut expected_paths = expected_paths();
    expected_paths.sort_unstable();
    assert_eq!(relative_file_paths(&output)?, expected_paths);
    for expected in EXPECTED_ARTIFACTS {
        if let Some(edge) = expected.edge {
            assert_quadrant_png(&output.join(expected.path), edge)?;
        }
    }
    assert_macos_contents_json(&output)?;
    assert_android_adaptive_xml(&output)?;
    assert_windows_ico(&output)?;
    assert_linux_desktop_entry(&output)?;
    assert!(
        snapshot_files(workspace.path())?
            .keys()
            .all(|path| !path.contains(".app-icon-toolkit-staging-"))
    );
    Ok(())
}

#[test]
fn omitting_monochrome_omits_v33_xml_and_monochrome_rasters() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    IconService::new().generate(workspace.path(), &target_job("generated", false)?)?;

    let paths = relative_file_paths(&workspace.path().join("generated"))?;
    assert!(!paths.iter().any(|path| path.contains("_monochrome.png")));
    assert!(!paths.iter().any(|path| path.contains("mipmap-anydpi-v33")));

    let mut expected = EXPECTED_ARTIFACTS
        .iter()
        .map(|artifact| artifact.path)
        .filter(|path| !path.contains("_monochrome.png"))
        .filter(|path| !path.contains("mipmap-anydpi-v33"))
        .collect::<Vec<_>>();
    expected.sort_unstable();
    assert_eq!(paths, expected);

    let v26 = fs::read_to_string(
        workspace
            .path()
            .join("generated/android/res/mipmap-anydpi-v26/ic_launcher.xml"),
    )?;
    assert!(!v26.contains("monochrome"));
    Ok(())
}

#[test]
fn transparent_foreground_hidden_rgb_does_not_bleed_into_generated_edges() -> TestResult {
    let workspace = tempdir()?;
    create_sources(workspace.path())?;
    let foreground = workspace.path().join("sources/foreground.png");
    let image = ImageBuffer::from_fn(1_024, 1_024, |x, _| {
        if x < 512 {
            Rgba([255_u8, 0, 0, 0])
        } else {
            Rgba([0_u8, 0, 255, 255])
        }
    });
    image.save(&foreground)?;

    IconService::new().generate(workspace.path(), &all_target_job("generated")?)?;
    let rendered = decode_png(
        &workspace
            .path()
            .join("generated/android/res/mipmap-mdpi/ic_launcher_foreground.png"),
    )?;
    assert!(rendered.pixels().any(|pixel| pixel.0[3] == 0));
    assert!(rendered.pixels().any(|pixel| pixel.0[3] == u8::MAX));
    assert!(
        rendered
            .pixels()
            .filter(|pixel| pixel.0[3] > 0)
            .all(|pixel| pixel.0[0] <= 2),
        "hidden red RGB bled into a visible resampled pixel"
    );
    Ok(())
}

fn expected_paths() -> Vec<String> {
    EXPECTED_ARTIFACTS
        .iter()
        .map(|artifact| artifact.path.to_owned())
        .collect()
}

fn decode_png(path: &Path) -> TestResult<RgbaImage> {
    let image = ImageReader::open(path)?.with_guessed_format()?.decode()?;
    Ok(image.to_rgba8())
}

fn assert_quadrant_png(path: &Path, expected_edge: u32) -> TestResult {
    let image = decode_png(path)?;
    assert_eq!(image.dimensions(), (expected_edge, expected_edge));

    let low = expected_edge / 4;
    let high = expected_edge.saturating_mul(3) / 4;
    assert_pixel_near(*image.get_pixel(low, low), RED);
    assert_pixel_near(*image.get_pixel(high, low), GREEN);
    assert_pixel_near(*image.get_pixel(low, high), BLUE);
    assert_pixel_near(*image.get_pixel(high, high), YELLOW);
    Ok(())
}

fn assert_quadrant_rgba(data: &[u8], expected_edge: u32) -> TestResult {
    let expected_len = usize::try_from(expected_edge)?
        .checked_mul(usize::try_from(expected_edge)?)
        .and_then(|pixels| pixels.checked_mul(4))
        .ok_or_else(|| io::Error::other("test image byte length overflow"))?;
    assert_eq!(data.len(), expected_len);

    let low = expected_edge / 4;
    let high = expected_edge.saturating_mul(3) / 4;
    assert_pixel_near(rgba_at(data, expected_edge, low, low)?, RED);
    assert_pixel_near(rgba_at(data, expected_edge, high, low)?, GREEN);
    assert_pixel_near(rgba_at(data, expected_edge, low, high)?, BLUE);
    assert_pixel_near(rgba_at(data, expected_edge, high, high)?, YELLOW);
    Ok(())
}

fn relative_file_paths(root: &Path) -> TestResult<Vec<String>> {
    Ok(snapshot_files(root)?.into_keys().collect())
}

fn assert_macos_contents_json(output: &Path) -> TestResult {
    let contents_path = output.join("macos/Assets.appiconset/Contents.json");
    let contents: Value = serde_json::from_slice(&fs::read(contents_path)?)?;
    let info = contents
        .get("info")
        .and_then(Value::as_object)
        .ok_or_else(|| io::Error::other("Contents.json omitted object field `info`"))?;
    assert_eq!(info.get("version").and_then(Value::as_u64), Some(1));
    assert!(
        info.get("author")
            .and_then(Value::as_str)
            .is_some_and(|author| !author.is_empty())
    );

    let images = contents
        .get("images")
        .and_then(Value::as_array)
        .ok_or_else(|| io::Error::other("Contents.json omitted array field `images`"))?;
    let mut actual = images
        .iter()
        .map(|image| {
            Ok((
                json_string(image, "filename")?,
                json_string(image, "idiom")?,
                json_string(image, "size")?,
                json_string(image, "scale")?,
            ))
        })
        .collect::<TestResult<Vec<_>>>()?;
    actual.sort_unstable();

    let mut expected = vec![
        ("icon_16x16.png", "mac", "16x16", "1x"),
        ("icon_16x16@2x.png", "mac", "16x16", "2x"),
        ("icon_32x32.png", "mac", "32x32", "1x"),
        ("icon_32x32@2x.png", "mac", "32x32", "2x"),
        ("icon_128x128.png", "mac", "128x128", "1x"),
        ("icon_128x128@2x.png", "mac", "128x128", "2x"),
        ("icon_256x256.png", "mac", "256x256", "1x"),
        ("icon_256x256@2x.png", "mac", "256x256", "2x"),
        ("icon_512x512.png", "mac", "512x512", "1x"),
        ("icon_512x512@2x.png", "mac", "512x512", "2x"),
    ];
    expected.sort_unstable();
    assert_eq!(actual, expected);
    Ok(())
}

fn assert_android_adaptive_xml(output: &Path) -> TestResult {
    let v26 = fs::read_to_string(output.join("android/res/mipmap-anydpi-v26/ic_launcher.xml"))?;
    assert!(v26.contains("<adaptive-icon"));
    assert!(v26.contains("xmlns:android=\"http://schemas.android.com/apk/res/android\""));
    assert!(v26.contains("android:drawable=\"@mipmap/ic_launcher_background\""));
    assert!(v26.contains("android:drawable=\"@mipmap/ic_launcher_foreground\""));
    assert!(!v26.contains("monochrome"));

    let v33 = fs::read_to_string(output.join("android/res/mipmap-anydpi-v33/ic_launcher.xml"))?;
    assert!(v33.contains("<adaptive-icon"));
    assert!(v33.contains("android:drawable=\"@mipmap/ic_launcher_background\""));
    assert!(v33.contains("android:drawable=\"@mipmap/ic_launcher_foreground\""));
    assert!(v33.contains("android:drawable=\"@mipmap/ic_launcher_monochrome\""));
    assert_eq!(v33.matches("<monochrome").count(), 1);
    Ok(())
}

fn assert_windows_ico(output: &Path) -> TestResult {
    let icon = IconDir::read(BufReader::new(File::open(
        output.join("windows/icon-probe.ico"),
    )?))?;
    let actual_sizes = icon
        .entries()
        .iter()
        .map(|entry| (entry.width(), entry.height()))
        .collect::<Vec<_>>();
    assert_eq!(
        actual_sizes,
        vec![(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)]
    );

    for entry in icon.entries() {
        let decoded = entry.decode()?;
        assert_eq!(decoded.width(), entry.width());
        assert_eq!(decoded.height(), entry.height());
        assert_quadrant_rgba(decoded.rgba_data(), entry.width())?;
    }
    Ok(())
}

fn assert_linux_desktop_entry(output: &Path) -> TestResult {
    let text =
        fs::read_to_string(output.join("linux/share/applications/com.example.IconProbe.desktop"))?;
    let mut lines = text.lines();
    assert_eq!(lines.next(), Some("[Desktop Entry]"));

    let mut fields = BTreeMap::new();
    for line in lines.filter(|line| !line.is_empty()) {
        let (key, value) = line
            .split_once('=')
            .ok_or_else(|| io::Error::other(format!("malformed desktop entry line: {line}")))?;
        assert!(fields.insert(key, value).is_none(), "duplicate key `{key}`");
    }
    assert_eq!(fields.get("Type"), Some(&"Application"));
    assert_eq!(fields.get("Name"), Some(&"Icon Probe"));
    assert_eq!(fields.get("Exec"), Some(&"icon-probe"));
    assert_eq!(fields.get("Icon"), Some(&"com.example.IconProbe"));
    assert_eq!(fields.get("Terminal"), Some(&"false"));
    Ok(())
}

fn json_string<'a>(value: &'a Value, field: &str) -> TestResult<&'a str> {
    value.get(field).and_then(Value::as_str).ok_or_else(|| {
        io::Error::other(format!(
            "Contents.json image omitted string field `{field}`"
        ))
        .into()
    })
}

fn assert_pixel_near(actual: Rgba<u8>, expected: Rgba<u8>) {
    for (actual_channel, expected_channel) in actual.0.into_iter().zip(expected.0) {
        assert!(
            actual_channel.abs_diff(expected_channel) <= 2,
            "actual pixel {actual:?} differed from expected {expected:?}"
        );
    }
}

fn rgba_at(data: &[u8], width: u32, x: u32, y: u32) -> TestResult<Rgba<u8>> {
    let pixel_index = usize::try_from(y)?
        .checked_mul(usize::try_from(width)?)
        .and_then(|row| row.checked_add(usize::try_from(x).ok()?))
        .and_then(|pixel| pixel.checked_mul(4))
        .ok_or_else(|| io::Error::other("test pixel offset overflow"))?;
    let end = pixel_index
        .checked_add(4)
        .ok_or_else(|| io::Error::other("test pixel end offset overflow"))?;
    let channels = data
        .get(pixel_index..end)
        .ok_or_else(|| io::Error::other("test pixel offset outside RGBA data"))?;
    Ok(Rgba([channels[0], channels[1], channels[2], channels[3]]))
}
