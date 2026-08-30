//! Windows MSIX application icon asset planning and rendering.

use app_icon_domain::{ArtifactKind, ArtifactPlan, PlatformProfile, ProfilePlan, RelativePath};
use image::RgbaImage;

use crate::{
    EngineError,
    exporters::{RenderedArtifact, profile},
    render::{PngCache, RasterSource},
};

const ASSET_ROOT: &str = "windows/msix/Assets";
const TARGET_SIZES: [u32; 14] = [16, 20, 24, 30, 32, 36, 40, 48, 60, 64, 72, 80, 96, 256];
const TARGET_SUFFIXES: [&str; 3] = ["", "_altform-unplated", "_altform-lightunplated"];
const SCALE_QUALIFIERS: [u16; 5] = [100, 125, 150, 200, 400];
const SCALED_ASSETS: [ScaledAsset; 3] = [
    ScaledAsset {
        stem: "AppList",
        pixels: [44, 55, 66, 88, 176],
    },
    ScaledAsset {
        stem: "MedTile",
        pixels: [150, 188, 225, 300, 600],
    },
    ScaledAsset {
        stem: "StoreLogo",
        pixels: [50, 63, 75, 100, 200],
    },
];

pub(crate) const ARTIFACT_COUNT: usize =
    TARGET_SIZES.len() * TARGET_SUFFIXES.len() + SCALE_QUALIFIERS.len() * SCALED_ASSETS.len();

#[derive(Debug, Clone, Copy)]
struct ScaledAsset {
    stem: &'static str,
    pixels: [u32; SCALE_QUALIFIERS.len()],
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct AssetRecipe {
    filename: String,
    pixels: u32,
}

pub(crate) fn plan() -> Result<ProfilePlan, EngineError> {
    let artifacts = asset_recipes()
        .into_iter()
        .map(|recipe| {
            Ok(ArtifactPlan::raster(
                asset_path(&recipe.filename)?,
                ArtifactKind::Png,
                recipe.pixels,
                recipe.pixels,
            ))
        })
        .collect::<Result<Vec<_>, EngineError>>()?;

    Ok(profile(PlatformProfile::WindowsMsixAssets, artifacts))
}

pub(crate) fn render(
    source_image: &RgbaImage,
    cache: &mut PngCache,
) -> Result<Vec<RenderedArtifact>, EngineError> {
    asset_recipes()
        .into_iter()
        .map(|recipe| {
            let path = asset_path(&recipe.filename)?;
            let bytes = cache
                .png(RasterSource::Flattened, source_image, recipe.pixels)
                .map_err(|source| EngineError::ArtifactEncode {
                    path: path.clone(),
                    source,
                })?;
            Ok(RenderedArtifact::new(path, bytes))
        })
        .collect()
}

fn asset_recipes() -> Vec<AssetRecipe> {
    let mut recipes = Vec::with_capacity(ARTIFACT_COUNT);
    for size in TARGET_SIZES {
        for suffix in TARGET_SUFFIXES {
            recipes.push(AssetRecipe {
                filename: format!("AppList.targetsize-{size}{suffix}.png"),
                pixels: size,
            });
        }
    }
    for scaled_asset in SCALED_ASSETS {
        for (scale, pixels) in SCALE_QUALIFIERS.into_iter().zip(scaled_asset.pixels) {
            recipes.push(AssetRecipe {
                filename: format!("{}.scale-{scale}.png", scaled_asset.stem),
                pixels,
            });
        }
    }
    recipes
}

fn asset_path(filename: &str) -> Result<RelativePath, EngineError> {
    RelativePath::new(format!("{ASSET_ROOT}/{filename}")).map_err(EngineError::from)
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::{ARTIFACT_COUNT, asset_recipes};

    #[test]
    fn recipes_cover_the_complete_fixed_matrix_without_collisions() {
        let recipes = asset_recipes();
        assert_eq!(recipes.len(), ARTIFACT_COUNT);
        assert_eq!(ARTIFACT_COUNT, 57);
        assert_eq!(
            recipes
                .iter()
                .map(|recipe| recipe.filename.as_str())
                .collect::<BTreeSet<_>>()
                .len(),
            ARTIFACT_COUNT
        );
        assert!(recipes.iter().any(|recipe| {
            recipe.filename == "AppList.targetsize-16.png" && recipe.pixels == 16
        }));
        assert!(recipes.iter().any(|recipe| {
            recipe.filename == "AppList.targetsize-256_altform-lightunplated.png"
                && recipe.pixels == 256
        }));
        assert!(
            recipes
                .iter()
                .any(|recipe| recipe.filename == "MedTile.scale-400.png" && recipe.pixels == 600)
        );
        assert!(
            recipes.iter().any(|recipe| {
                recipe.filename == "StoreLogo.scale-125.png" && recipe.pixels == 63
            })
        );
    }
}
