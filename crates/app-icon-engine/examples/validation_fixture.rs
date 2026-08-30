//! Generates a deterministic four-platform fixture for native-tool validation.

use std::{env, error::Error, fs, io, path::Path};

use app_icon_domain::{
    AdaptiveSources, AndroidResourceName, ApplicationId, ArtifactName, DisplayName, ExecutableName,
    IconJob, IconSources, RelativePath, TargetSpec,
};
use app_icon_engine::IconService;
use image::{ImageBuffer, Rgba};

const SOURCE_EDGE: u32 = 1_024;
const EXPECTED_ARTIFACTS: usize = 101;

fn main() -> Result<(), Box<dyn Error + Send + Sync>> {
    let mut arguments = env::args_os();
    let _program = arguments.next();
    let workspace_root = arguments.next().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "usage: validation_fixture <workspace-root>",
        )
    })?;
    if arguments.next().is_some() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "validation_fixture accepts exactly one workspace root",
        )
        .into());
    }

    let workspace_root = Path::new(&workspace_root);
    fs::create_dir_all(workspace_root)?;
    write_sources(workspace_root)?;

    let job = validation_job()?;
    let report = IconService::new().generate(workspace_root, &job)?;
    if report.artifacts().len() != EXPECTED_ARTIFACTS {
        return Err(io::Error::other(format!(
            "fixture generated {} artifacts; expected {EXPECTED_ARTIFACTS}",
            report.artifacts().len()
        ))
        .into());
    }

    println!("{}", workspace_root.join("generated").display());
    Ok(())
}

fn write_sources(workspace_root: &Path) -> Result<(), Box<dyn Error + Send + Sync>> {
    let source_directory = workspace_root.join("sources");
    fs::create_dir_all(&source_directory)?;
    let image = ImageBuffer::from_fn(SOURCE_EDGE, SOURCE_EDGE, |x, y| {
        match (x < SOURCE_EDGE / 2, y < SOURCE_EDGE / 2) {
            (true, true) => Rgba([239_u8, 43, 57, 255]),
            (false, true) => Rgba([40_u8, 180, 99, 255]),
            (true, false) => Rgba([45_u8, 101, 220, 255]),
            (false, false) => Rgba([246_u8, 196, 52, 255]),
        }
    });

    for filename in [
        "flattened.png",
        "foreground.png",
        "background.png",
        "monochrome.png",
    ] {
        image.save(source_directory.join(filename))?;
    }
    Ok(())
}

fn validation_job() -> Result<IconJob, Box<dyn Error + Send + Sync>> {
    let adaptive = AdaptiveSources::new(
        RelativePath::new("sources/foreground.png")?,
        RelativePath::new("sources/background.png")?,
        Some(RelativePath::new("sources/monochrome.png")?),
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
        TargetSpec::WindowsMsixAssets,
        TargetSpec::LinuxXdg {
            application_id: ApplicationId::new("com.example.IconProbe")?,
            display_name: DisplayName::new("Icon Probe")?,
            executable: ExecutableName::new("icon-probe")?,
        },
    ];

    Ok(IconJob::new(
        RelativePath::new("generated")?,
        sources,
        targets,
    )?)
}
