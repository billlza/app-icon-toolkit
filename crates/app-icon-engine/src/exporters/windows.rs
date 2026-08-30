//! Windows ICO planning.

use app_icon_domain::{
    ArtifactKind, ArtifactName, ArtifactPlan, PlatformProfile, ProfilePlan, RelativePath,
};
use image::RgbaImage;

use crate::{
    EngineError,
    exporters::{RenderedArtifact, profile},
    render::{PngCache, RasterSource},
};

pub(crate) const FRAME_SIZES: [u32; 5] = [16, 24, 32, 48, 256];

pub(crate) fn plan(file_stem: &ArtifactName) -> Result<ProfilePlan, EngineError> {
    let path = ico_path(file_stem)?;
    Ok(profile(
        PlatformProfile::WindowsIco,
        vec![ArtifactPlan::document(path, ArtifactKind::Ico)],
    ))
}

pub(crate) fn ico_path(file_stem: &ArtifactName) -> Result<RelativePath, EngineError> {
    RelativePath::new(format!("windows/{}.ico", file_stem.as_str())).map_err(EngineError::from)
}

pub(crate) fn render(
    file_stem: &ArtifactName,
    source_image: &RgbaImage,
    cache: &mut PngCache,
) -> Result<Vec<RenderedArtifact>, EngineError> {
    let path = ico_path(file_stem)?;
    let bytes = cache
        .ico(RasterSource::Flattened, source_image, &FRAME_SIZES)
        .map_err(|source| EngineError::ArtifactEncode {
            path: path.clone(),
            source,
        })?;
    Ok(vec![RenderedArtifact::new(path, bytes)])
}
