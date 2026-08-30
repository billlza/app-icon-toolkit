//! Android launcher-resource planning.

use app_icon_domain::{
    AdaptiveSources, AndroidResourceName, ArtifactKind, ArtifactPlan, DomainError, PlatformProfile,
    ProfilePlan, RelativePath,
};
use image::RgbaImage;

use crate::{
    EngineError,
    exporters::{RenderedArtifact, profile},
    render::{PngCache, RasterSource},
    source::PreparedAdaptiveSources,
};

pub(crate) const DENSITIES: [(&str, u32, u32); 5] = [
    ("mdpi", 48, 108),
    ("hdpi", 72, 162),
    ("xhdpi", 96, 216),
    ("xxhdpi", 144, 324),
    ("xxxhdpi", 192, 432),
];

pub(crate) fn plan(
    resource_name: &AndroidResourceName,
    adaptive: Option<&AdaptiveSources>,
) -> Result<ProfilePlan, EngineError> {
    let adaptive = adaptive.ok_or(DomainError::MissingAdaptiveSources)?;
    let mut artifacts = Vec::with_capacity(if adaptive.monochrome().is_some() {
        22
    } else {
        16
    });
    for (density, legacy_size, adaptive_size) in DENSITIES {
        artifacts.push(png_plan(resource_name, density, None, legacy_size)?);
        artifacts.push(png_plan(
            resource_name,
            density,
            Some("foreground"),
            adaptive_size,
        )?);
        artifacts.push(png_plan(
            resource_name,
            density,
            Some("background"),
            adaptive_size,
        )?);
        if adaptive.monochrome().is_some() {
            artifacts.push(png_plan(
                resource_name,
                density,
                Some("monochrome"),
                adaptive_size,
            )?);
        }
    }

    artifacts.push(ArtifactPlan::document(
        xml_path(resource_name, 26)?,
        ArtifactKind::Xml,
    ));
    if adaptive.monochrome().is_some() {
        artifacts.push(ArtifactPlan::document(
            xml_path(resource_name, 33)?,
            ArtifactKind::Xml,
        ));
    }

    Ok(profile(PlatformProfile::AndroidAdaptive, artifacts))
}

pub(crate) fn png_path(
    resource_name: &AndroidResourceName,
    density: &str,
    layer: Option<&str>,
) -> Result<RelativePath, EngineError> {
    let suffix = layer.map_or_else(String::new, |value| format!("_{value}"));
    RelativePath::new(format!(
        "android/res/mipmap-{density}/{}{suffix}.png",
        resource_name.as_str()
    ))
    .map_err(EngineError::from)
}

pub(crate) fn xml_path(
    resource_name: &AndroidResourceName,
    api_level: u32,
) -> Result<RelativePath, EngineError> {
    RelativePath::new(format!(
        "android/res/mipmap-anydpi-v{api_level}/{}.xml",
        resource_name.as_str()
    ))
    .map_err(EngineError::from)
}

fn png_plan(
    resource_name: &AndroidResourceName,
    density: &str,
    layer: Option<&str>,
    pixels: u32,
) -> Result<ArtifactPlan, EngineError> {
    Ok(ArtifactPlan::raster(
        png_path(resource_name, density, layer)?,
        ArtifactKind::Png,
        pixels,
        pixels,
    ))
}

pub(crate) fn render(
    resource_name: &AndroidResourceName,
    adaptive: Option<&PreparedAdaptiveSources>,
    flattened: &RgbaImage,
    cache: &mut PngCache,
) -> Result<Vec<RenderedArtifact>, EngineError> {
    let adaptive = adaptive.ok_or(DomainError::MissingAdaptiveSources)?;
    let foreground = &adaptive.foreground.image;
    let background = &adaptive.background.image;
    let monochrome = adaptive.monochrome.as_ref().map(|source| &source.image);

    let mut artifacts = Vec::with_capacity(if monochrome.is_some() { 22 } else { 16 });
    for (density, legacy_size, adaptive_size) in DENSITIES {
        artifacts.push(render_png(
            resource_name,
            density,
            None,
            flattened,
            legacy_size,
            RasterSource::Flattened,
            cache,
        )?);
        artifacts.push(render_png(
            resource_name,
            density,
            Some("foreground"),
            foreground,
            adaptive_size,
            RasterSource::AdaptiveForeground,
            cache,
        )?);
        artifacts.push(render_png(
            resource_name,
            density,
            Some("background"),
            background,
            adaptive_size,
            RasterSource::AdaptiveBackground,
            cache,
        )?);
        if let Some(monochrome) = monochrome {
            artifacts.push(render_png(
                resource_name,
                density,
                Some("monochrome"),
                monochrome,
                adaptive_size,
                RasterSource::AdaptiveMonochrome,
                cache,
            )?);
        }
    }

    let v26_path = xml_path(resource_name, 26)?;
    artifacts.push(RenderedArtifact::new(
        v26_path,
        adaptive_xml(resource_name, false).into_bytes(),
    ));
    if monochrome.is_some() {
        let v33_path = xml_path(resource_name, 33)?;
        artifacts.push(RenderedArtifact::new(
            v33_path,
            adaptive_xml(resource_name, true).into_bytes(),
        ));
    }
    Ok(artifacts)
}

fn render_png(
    resource_name: &AndroidResourceName,
    density: &str,
    layer: Option<&str>,
    source_image: &RgbaImage,
    pixels: u32,
    source_key: RasterSource,
    cache: &mut PngCache,
) -> Result<RenderedArtifact, EngineError> {
    let path = png_path(resource_name, density, layer)?;
    let bytes = cache
        .png(source_key, source_image, pixels)
        .map_err(|source| EngineError::ArtifactEncode {
            path: path.clone(),
            source,
        })?;
    Ok(RenderedArtifact::new(path, bytes))
}

fn adaptive_xml(resource_name: &AndroidResourceName, include_monochrome: bool) -> String {
    let name = resource_name.as_str();
    let monochrome = if include_monochrome {
        format!("\n    <monochrome android:drawable=\"@mipmap/{name}_monochrome\" />")
    } else {
        String::new()
    };
    format!(
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<adaptive-icon xmlns:android=\"http://schemas.android.com/apk/res/android\">\n    <background android:drawable=\"@mipmap/{name}_background\" />\n    <foreground android:drawable=\"@mipmap/{name}_foreground\" />{monochrome}\n</adaptive-icon>\n"
    )
}
