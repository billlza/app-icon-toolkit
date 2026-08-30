use std::collections::BTreeMap;
use std::error::Error;
use std::fs;
use std::io;
use std::path::Path;

use image::{ImageBuffer, Rgba};

use app_icon_domain::{
    AdaptiveSources, AndroidResourceName, ApplicationId, ArtifactKind, ArtifactName, DisplayName,
    ExecutableName, IconJob, IconSources, RelativePath, TargetSpec,
};

pub(crate) type TestResult<T = ()> = Result<T, Box<dyn Error + Send + Sync>>;

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
mod publication;

const SOURCE_EDGE: u32 = 1_024;
const RED: Rgba<u8> = Rgba([239, 43, 57, 255]);
const GREEN: Rgba<u8> = Rgba([40, 180, 99, 255]);
const BLUE: Rgba<u8> = Rgba([45, 101, 220, 255]);
const YELLOW: Rgba<u8> = Rgba([246, 196, 52, 255]);

pub(crate) fn write_quadrant_png(path: &Path) -> TestResult {
    write_quadrant_png_with_dimensions(path, SOURCE_EDGE, SOURCE_EDGE, u8::MAX)
}

pub(crate) fn write_quadrant_png_with_dimensions(
    path: &Path,
    width: u32,
    height: u32,
    alpha: u8,
) -> TestResult {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }

    let image = ImageBuffer::from_fn(width, height, |x, y| {
        let mut pixel = match (x < width / 2, y < height / 2) {
            (true, true) => RED,
            (false, true) => GREEN,
            (true, false) => BLUE,
            (false, false) => YELLOW,
        };
        pixel.0[3] = alpha;
        pixel
    });
    image.save(path)?;
    Ok(())
}

pub(crate) fn snapshot_files(root: &Path) -> TestResult<BTreeMap<String, Vec<u8>>> {
    let mut snapshot = BTreeMap::new();
    collect_files(root, root, &mut snapshot)?;
    Ok(snapshot)
}

fn collect_files(
    root: &Path,
    current: &Path,
    snapshot: &mut BTreeMap<String, Vec<u8>>,
) -> TestResult {
    let mut entries = fs::read_dir(current)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(std::fs::DirEntry::file_name);

    for entry in entries {
        let file_type = entry.file_type()?;
        let path = entry.path();
        if file_type.is_dir() {
            collect_files(root, &path, snapshot)?;
        } else if file_type.is_file() {
            let relative = portable_relative_path(root, &path)?;
            snapshot.insert(relative, fs::read(path)?);
        } else {
            return Err(io::Error::other(format!(
                "unexpected non-file artifact: {}",
                path.display()
            ))
            .into());
        }
    }
    Ok(())
}

pub(crate) fn portable_relative_path(root: &Path, path: &Path) -> TestResult<String> {
    let relative = path.strip_prefix(root)?;
    let mut parts = Vec::new();
    for component in relative.components() {
        let part = component.as_os_str().to_str().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "artifact path is not UTF-8")
        })?;
        parts.push(part);
    }
    Ok(parts.join("/"))
}

pub(crate) fn create_sources(root: &Path) -> TestResult {
    let flattened = root.join("sources/flattened.png");
    let foreground = root.join("sources/foreground.png");
    let background = root.join("sources/background.png");
    let monochrome = root.join("sources/monochrome.png");

    for path in [&flattened, &foreground, &background, &monochrome] {
        write_quadrant_png(path)?;
    }

    Ok(())
}

pub(crate) fn all_target_job(output_directory: &str) -> TestResult<IconJob> {
    target_job(output_directory, true)
}

fn target_job(output_directory: &str, include_monochrome: bool) -> TestResult<IconJob> {
    let adaptive = AdaptiveSources::new(
        RelativePath::new("sources/foreground.png")?,
        RelativePath::new("sources/background.png")?,
        include_monochrome
            .then(|| RelativePath::new("sources/monochrome.png"))
            .transpose()?,
    );
    let sources = IconSources::new(RelativePath::new("sources/flattened.png")?, Some(adaptive));
    let targets = vec![
        TargetSpec::MacOsAppIconSet {
            icon_set_name: ArtifactName::new("Assets")?,
        },
        TargetSpec::AndroidAdaptive {
            resource_name: AndroidResourceName::new("ic_launcher")?,
        },
        TargetSpec::WindowsIco {
            file_stem: ArtifactName::new("icon-probe")?,
        },
        TargetSpec::LinuxXdg {
            application_id: ApplicationId::new("com.example.IconProbe")?,
            display_name: DisplayName::new("Icon Probe")?,
            executable: ExecutableName::new("icon-probe")?,
        },
    ];

    Ok(IconJob::new(
        RelativePath::new(output_directory)?,
        sources,
        targets,
    )?)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ExpectedArtifact {
    pub(crate) path: &'static str,
    pub(crate) kind: ArtifactKind,
    pub(crate) edge: Option<u32>,
}

pub(crate) const EXPECTED_ARTIFACTS: &[ExpectedArtifact] = &[
    ExpectedArtifact {
        path: "macos/Assets.appiconset/Contents.json",
        kind: ArtifactKind::Json,
        edge: None,
    },
    ExpectedArtifact {
        path: "macos/Assets.appiconset/icon_16x16.png",
        kind: ArtifactKind::Png,
        edge: Some(16),
    },
    ExpectedArtifact {
        path: "macos/Assets.appiconset/icon_16x16@2x.png",
        kind: ArtifactKind::Png,
        edge: Some(32),
    },
    ExpectedArtifact {
        path: "macos/Assets.appiconset/icon_32x32.png",
        kind: ArtifactKind::Png,
        edge: Some(32),
    },
    ExpectedArtifact {
        path: "macos/Assets.appiconset/icon_32x32@2x.png",
        kind: ArtifactKind::Png,
        edge: Some(64),
    },
    ExpectedArtifact {
        path: "macos/Assets.appiconset/icon_128x128.png",
        kind: ArtifactKind::Png,
        edge: Some(128),
    },
    ExpectedArtifact {
        path: "macos/Assets.appiconset/icon_128x128@2x.png",
        kind: ArtifactKind::Png,
        edge: Some(256),
    },
    ExpectedArtifact {
        path: "macos/Assets.appiconset/icon_256x256.png",
        kind: ArtifactKind::Png,
        edge: Some(256),
    },
    ExpectedArtifact {
        path: "macos/Assets.appiconset/icon_256x256@2x.png",
        kind: ArtifactKind::Png,
        edge: Some(512),
    },
    ExpectedArtifact {
        path: "macos/Assets.appiconset/icon_512x512.png",
        kind: ArtifactKind::Png,
        edge: Some(512),
    },
    ExpectedArtifact {
        path: "macos/Assets.appiconset/icon_512x512@2x.png",
        kind: ArtifactKind::Png,
        edge: Some(1_024),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-mdpi/ic_launcher.png",
        kind: ArtifactKind::Png,
        edge: Some(48),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-hdpi/ic_launcher.png",
        kind: ArtifactKind::Png,
        edge: Some(72),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xhdpi/ic_launcher.png",
        kind: ArtifactKind::Png,
        edge: Some(96),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xxhdpi/ic_launcher.png",
        kind: ArtifactKind::Png,
        edge: Some(144),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xxxhdpi/ic_launcher.png",
        kind: ArtifactKind::Png,
        edge: Some(192),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-mdpi/ic_launcher_foreground.png",
        kind: ArtifactKind::Png,
        edge: Some(108),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-hdpi/ic_launcher_foreground.png",
        kind: ArtifactKind::Png,
        edge: Some(162),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xhdpi/ic_launcher_foreground.png",
        kind: ArtifactKind::Png,
        edge: Some(216),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xxhdpi/ic_launcher_foreground.png",
        kind: ArtifactKind::Png,
        edge: Some(324),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xxxhdpi/ic_launcher_foreground.png",
        kind: ArtifactKind::Png,
        edge: Some(432),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-mdpi/ic_launcher_background.png",
        kind: ArtifactKind::Png,
        edge: Some(108),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-hdpi/ic_launcher_background.png",
        kind: ArtifactKind::Png,
        edge: Some(162),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xhdpi/ic_launcher_background.png",
        kind: ArtifactKind::Png,
        edge: Some(216),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xxhdpi/ic_launcher_background.png",
        kind: ArtifactKind::Png,
        edge: Some(324),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xxxhdpi/ic_launcher_background.png",
        kind: ArtifactKind::Png,
        edge: Some(432),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-mdpi/ic_launcher_monochrome.png",
        kind: ArtifactKind::Png,
        edge: Some(108),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-hdpi/ic_launcher_monochrome.png",
        kind: ArtifactKind::Png,
        edge: Some(162),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xhdpi/ic_launcher_monochrome.png",
        kind: ArtifactKind::Png,
        edge: Some(216),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xxhdpi/ic_launcher_monochrome.png",
        kind: ArtifactKind::Png,
        edge: Some(324),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-xxxhdpi/ic_launcher_monochrome.png",
        kind: ArtifactKind::Png,
        edge: Some(432),
    },
    ExpectedArtifact {
        path: "android/res/mipmap-anydpi-v26/ic_launcher.xml",
        kind: ArtifactKind::Xml,
        edge: None,
    },
    ExpectedArtifact {
        path: "android/res/mipmap-anydpi-v33/ic_launcher.xml",
        kind: ArtifactKind::Xml,
        edge: None,
    },
    ExpectedArtifact {
        path: "windows/icon-probe.ico",
        kind: ArtifactKind::Ico,
        edge: None,
    },
    ExpectedArtifact {
        path: "linux/share/icons/hicolor/16x16/apps/com.example.IconProbe.png",
        kind: ArtifactKind::Png,
        edge: Some(16),
    },
    ExpectedArtifact {
        path: "linux/share/icons/hicolor/22x22/apps/com.example.IconProbe.png",
        kind: ArtifactKind::Png,
        edge: Some(22),
    },
    ExpectedArtifact {
        path: "linux/share/icons/hicolor/24x24/apps/com.example.IconProbe.png",
        kind: ArtifactKind::Png,
        edge: Some(24),
    },
    ExpectedArtifact {
        path: "linux/share/icons/hicolor/32x32/apps/com.example.IconProbe.png",
        kind: ArtifactKind::Png,
        edge: Some(32),
    },
    ExpectedArtifact {
        path: "linux/share/icons/hicolor/48x48/apps/com.example.IconProbe.png",
        kind: ArtifactKind::Png,
        edge: Some(48),
    },
    ExpectedArtifact {
        path: "linux/share/icons/hicolor/64x64/apps/com.example.IconProbe.png",
        kind: ArtifactKind::Png,
        edge: Some(64),
    },
    ExpectedArtifact {
        path: "linux/share/icons/hicolor/128x128/apps/com.example.IconProbe.png",
        kind: ArtifactKind::Png,
        edge: Some(128),
    },
    ExpectedArtifact {
        path: "linux/share/icons/hicolor/256x256/apps/com.example.IconProbe.png",
        kind: ArtifactKind::Png,
        edge: Some(256),
    },
    ExpectedArtifact {
        path: "linux/share/icons/hicolor/512x512/apps/com.example.IconProbe.png",
        kind: ArtifactKind::Png,
        edge: Some(512),
    },
    ExpectedArtifact {
        path: "linux/share/applications/com.example.IconProbe.desktop",
        kind: ArtifactKind::DesktopEntry,
        edge: None,
    },
];
