//! macOS app-icon-set planning and rendering.

use app_icon_domain::{
    ArtifactKind, ArtifactName, ArtifactPlan, PlatformProfile, ProfilePlan, RelativePath,
};
use image::RgbaImage;
use serde_json::json;

use crate::{
    EngineError,
    exporters::{RenderedArtifact, profile},
    render::{PngCache, RasterSource},
};

pub(crate) const SLOTS: [(&str, u32, &str, &str); 10] = [
    ("icon_16x16.png", 16, "16x16", "1x"),
    ("icon_16x16@2x.png", 32, "16x16", "2x"),
    ("icon_32x32.png", 32, "32x32", "1x"),
    ("icon_32x32@2x.png", 64, "32x32", "2x"),
    ("icon_128x128.png", 128, "128x128", "1x"),
    ("icon_128x128@2x.png", 256, "128x128", "2x"),
    ("icon_256x256.png", 256, "256x256", "1x"),
    ("icon_256x256@2x.png", 512, "256x256", "2x"),
    ("icon_512x512.png", 512, "512x512", "1x"),
    ("icon_512x512@2x.png", 1024, "512x512", "2x"),
];

pub(crate) fn plan(icon_set_name: &ArtifactName) -> Result<ProfilePlan, EngineError> {
    let directory = icon_set_directory(icon_set_name)?;
    let mut artifacts = Vec::with_capacity(SLOTS.len() + 1);
    for (filename, pixels, _, _) in SLOTS {
        artifacts.push(ArtifactPlan::raster(
            directory.join_component(filename)?,
            ArtifactKind::Png,
            pixels,
            pixels,
        ));
    }
    artifacts.push(ArtifactPlan::document(
        directory.join_component("Contents.json")?,
        ArtifactKind::Json,
    ));
    Ok(profile(PlatformProfile::MacOsAppIconSet, artifacts))
}

pub(crate) fn icon_set_directory(
    icon_set_name: &ArtifactName,
) -> Result<RelativePath, EngineError> {
    RelativePath::new(format!("macos/{}.appiconset", icon_set_name.as_str()))
        .map_err(EngineError::from)
}

pub(crate) fn render(
    icon_set_name: &ArtifactName,
    source: &RgbaImage,
    cache: &mut PngCache,
) -> Result<Vec<RenderedArtifact>, EngineError> {
    let directory = icon_set_directory(icon_set_name)?;
    let mut artifacts = Vec::with_capacity(SLOTS.len() + 1);
    let mut images = Vec::with_capacity(SLOTS.len());

    for (filename, pixels, point_size, scale) in SLOTS {
        let path = directory.join_component(filename)?;
        let bytes = cache
            .png(RasterSource::Flattened, source, pixels)
            .map_err(|source| EngineError::ArtifactEncode {
                path: path.clone(),
                source,
            })?;
        artifacts.push(RenderedArtifact::new(path, bytes));
        images.push(json!({
            "filename": filename,
            "idiom": "mac",
            "scale": scale,
            "size": point_size,
        }));
    }

    let contents_path = directory.join_component("Contents.json")?;
    let contents = json!({
        "images": images,
        "info": {
            "author": "app-icon-toolkit",
            "version": 1,
        },
    });
    let mut bytes =
        serde_json::to_vec_pretty(&contents).map_err(|source| EngineError::ArtifactSerialize {
            path: contents_path.clone(),
            source,
        })?;
    bytes.push(b'\n');
    artifacts.push(RenderedArtifact::new(contents_path, bytes));
    Ok(artifacts)
}
