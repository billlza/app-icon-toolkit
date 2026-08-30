//! Freedesktop hicolor and desktop-entry planning.

use app_icon_domain::{
    ApplicationId, ArtifactKind, ArtifactPlan, DisplayName, ExecutableName, PlatformProfile,
    ProfilePlan, RelativePath,
};
use image::RgbaImage;

use crate::{
    EngineError,
    exporters::{RenderedArtifact, profile},
    render::{PngCache, RasterSource},
};

pub(crate) const ICON_SIZES: [u32; 9] = [16, 22, 24, 32, 48, 64, 128, 256, 512];

pub(crate) fn plan(application_id: &ApplicationId) -> Result<ProfilePlan, EngineError> {
    let mut artifacts = Vec::with_capacity(ICON_SIZES.len() + 1);
    for size in ICON_SIZES {
        artifacts.push(ArtifactPlan::raster(
            icon_path(application_id, size)?,
            ArtifactKind::Png,
            size,
            size,
        ));
    }
    artifacts.push(ArtifactPlan::document(
        desktop_path(application_id)?,
        ArtifactKind::DesktopEntry,
    ));
    Ok(profile(PlatformProfile::LinuxXdg, artifacts))
}

pub(crate) fn icon_path(
    application_id: &ApplicationId,
    size: u32,
) -> Result<RelativePath, EngineError> {
    RelativePath::new(format!(
        "linux/share/icons/hicolor/{size}x{size}/apps/{}.png",
        application_id.as_str()
    ))
    .map_err(EngineError::from)
}

pub(crate) fn desktop_path(application_id: &ApplicationId) -> Result<RelativePath, EngineError> {
    RelativePath::new(format!(
        "linux/share/applications/{}.desktop",
        application_id.as_str()
    ))
    .map_err(EngineError::from)
}

pub(crate) fn render(
    application_id: &ApplicationId,
    display_name: &DisplayName,
    executable: &ExecutableName,
    source_image: &RgbaImage,
    cache: &mut PngCache,
) -> Result<Vec<RenderedArtifact>, EngineError> {
    let mut artifacts = Vec::with_capacity(ICON_SIZES.len() + 1);
    for size in ICON_SIZES {
        let path = icon_path(application_id, size)?;
        let bytes = cache
            .png(RasterSource::Flattened, source_image, size)
            .map_err(|source| EngineError::ArtifactEncode {
                path: path.clone(),
                source,
            })?;
        artifacts.push(RenderedArtifact::new(path, bytes));
    }

    let desktop_path = desktop_path(application_id)?;
    let escaped_name = display_name.as_str().replace('\\', "\\\\");
    let desktop_entry = format!(
        "[Desktop Entry]\nVersion=1.0\nType=Application\nName={escaped_name}\nExec={}\nIcon={}\nTerminal=false\n",
        executable.as_str(),
        application_id.as_str()
    );
    artifacts.push(RenderedArtifact::new(
        desktop_path,
        desktop_entry.into_bytes(),
    ));
    Ok(artifacts)
}
