//! Shared raster and container rendering implementation.

use std::{
    collections::BTreeMap,
    io::{self, Cursor},
};

use ico::{IconDir, IconDirEntry, IconImage, ResourceType};
use image::{DynamicImage, GenericImageView, ImageFormat, Rgba, RgbaImage, imageops::FilterType};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub(crate) enum RasterSource {
    Flattened,
    AdaptiveForeground,
    AdaptiveBackground,
    AdaptiveMonochrome,
}

#[derive(Debug, Default)]
pub(crate) struct PngCache {
    entries: BTreeMap<(RasterSource, u32), Vec<u8>>,
    prepared: BTreeMap<RasterSource, PreparedRaster>,
}

#[derive(Debug)]
enum PreparedRaster {
    Opaque,
    Premultiplied(RgbaImage),
}

impl PngCache {
    pub(crate) fn png(
        &mut self,
        source: RasterSource,
        image: &RgbaImage,
        size: u32,
    ) -> io::Result<Vec<u8>> {
        if let Some(bytes) = self.entries.get(&(source, size)) {
            return Ok(bytes.clone());
        }

        let prepared = self
            .prepared
            .entry(source)
            .or_insert_with(|| prepare_raster(image));
        let resized = match prepared {
            PreparedRaster::Opaque => {
                image::imageops::resize(image, size, size, FilterType::CatmullRom)
            }
            PreparedRaster::Premultiplied(premultiplied) => {
                let resized =
                    image::imageops::resize(premultiplied, size, size, FilterType::CatmullRom);
                unpremultiply(resized)
            }
        };
        let bytes = encode_png(resized)?;
        self.entries.insert((source, size), bytes.clone());
        Ok(bytes)
    }

    pub(crate) fn ico(
        &mut self,
        source: RasterSource,
        image: &RgbaImage,
        sizes: &[u32],
    ) -> io::Result<Vec<u8>> {
        let mut directory = IconDir::new(ResourceType::Icon);
        for size in sizes {
            let encoded_png = self.png(source, image, *size)?;
            let icon_image = IconImage::read_png(Cursor::new(encoded_png))?;
            let entry = IconDirEntry::encode(&icon_image)?;
            directory.add_entry(entry);
        }
        let mut output = Vec::new();
        directory.write(&mut output)?;
        Ok(output)
    }
}

fn prepare_raster(image: &RgbaImage) -> PreparedRaster {
    if image.pixels().all(|pixel| pixel.0[3] == u8::MAX) {
        return PreparedRaster::Opaque;
    }

    let premultiplied = RgbaImage::from_fn(image.width(), image.height(), |x, y| {
        let pixel = image.get_pixel(x, y).0;
        let alpha = u16::from(pixel[3]);
        Rgba([
            premultiply_channel(pixel[0], alpha),
            premultiply_channel(pixel[1], alpha),
            premultiply_channel(pixel[2], alpha),
            pixel[3],
        ])
    });
    PreparedRaster::Premultiplied(premultiplied)
}

fn premultiply_channel(channel: u8, alpha: u16) -> u8 {
    let product = u16::from(channel) * alpha;
    ((product + 127) / 255) as u8
}

fn unpremultiply(mut image: RgbaImage) -> RgbaImage {
    for pixel in image.pixels_mut() {
        let alpha = u32::from(pixel.0[3]);
        if alpha == 0 {
            pixel.0[0] = 0;
            pixel.0[1] = 0;
            pixel.0[2] = 0;
            continue;
        }
        pixel.0[0] = unpremultiply_channel(pixel.0[0], alpha);
        pixel.0[1] = unpremultiply_channel(pixel.0[1], alpha);
        pixel.0[2] = unpremultiply_channel(pixel.0[2], alpha);
    }
    image
}

fn unpremultiply_channel(channel: u8, alpha: u32) -> u8 {
    let restored = (u32::from(channel) * 255 + alpha / 2) / alpha;
    restored.min(u32::from(u8::MAX)) as u8
}

fn encode_png(image: RgbaImage) -> io::Result<Vec<u8>> {
    let mut output = Cursor::new(Vec::new());
    DynamicImage::ImageRgba8(image)
        .write_to(&mut output, ImageFormat::Png)
        .map_err(io::Error::other)?;
    Ok(output.into_inner())
}

pub(crate) fn validate_png(bytes: &[u8], expected_size: u32) -> Result<(), String> {
    let image = image::load_from_memory_with_format(bytes, ImageFormat::Png)
        .map_err(|error| format!("PNG decode failed: {error}"))?;
    let dimensions = image.dimensions();
    if dimensions != (expected_size, expected_size) {
        return Err(format!(
            "PNG dimensions are {}x{}; expected {expected_size}x{expected_size}",
            dimensions.0, dimensions.1
        ));
    }
    Ok(())
}

pub(crate) fn validate_ico(bytes: &[u8], expected_sizes: &[u32]) -> Result<(), String> {
    let directory = IconDir::read(Cursor::new(bytes))
        .map_err(|error| format!("ICO directory decode failed: {error}"))?;
    if directory.resource_type() != ResourceType::Icon {
        return Err("ICO resource type is not icon".to_owned());
    }
    if directory.entries().len() != expected_sizes.len() {
        return Err(format!(
            "ICO has {} frames; expected {}",
            directory.entries().len(),
            expected_sizes.len()
        ));
    }
    for (entry, expected_size) in directory.entries().iter().zip(expected_sizes) {
        if entry.width() != *expected_size || entry.height() != *expected_size {
            return Err(format!(
                "ICO frame is {}x{}; expected {expected_size}x{expected_size}",
                entry.width(),
                entry.height()
            ));
        }
        entry
            .decode()
            .map_err(|error| format!("ICO frame decode failed: {error}"))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::{error::Error, io};

    use image::{ImageFormat, Rgba, RgbaImage, imageops::FilterType};

    use super::{PngCache, RasterSource, encode_png};

    type TestResult = Result<(), Box<dyn Error + Send + Sync>>;

    #[test]
    fn premultiplied_resize_does_not_bleed_hidden_rgb() -> TestResult {
        let mut source = RgbaImage::from_pixel(8, 8, Rgba([255, 0, 0, 0]));
        for y in 2..6 {
            for x in 2..6 {
                source.put_pixel(x, y, Rgba([0, 0, 255, 255]));
            }
        }

        let encoded = PngCache::default().png(RasterSource::Flattened, &source, 5)?;
        let resized = image::load_from_memory_with_format(&encoded, ImageFormat::Png)?.into_rgba8();
        let mut translucent_pixels = 0_u32;
        for pixel in resized.pixels() {
            let [red, green, blue, alpha] = pixel.0;
            if alpha > 0 && alpha < u8::MAX {
                translucent_pixels += 1;
                assert!(red <= 1, "hidden red leaked into edge pixel: {pixel:?}");
                assert!(green <= 1, "green leaked into edge pixel: {pixel:?}");
                assert!(blue >= 250, "blue edge was not preserved: {pixel:?}");
            }
        }
        if translucent_pixels == 0 {
            return Err(io::Error::other("fixture produced no translucent edge pixels").into());
        }
        Ok(())
    }

    #[test]
    fn opaque_resize_keeps_the_direct_encoding_path() -> TestResult {
        let source = RgbaImage::from_fn(16, 16, |x, y| {
            Rgba([(x * 11) as u8, (y * 13) as u8, ((x + y) * 7) as u8, 255])
        });
        let expected_image = image::imageops::resize(&source, 9, 9, FilterType::CatmullRom);
        let expected = encode_png(expected_image)?;
        let actual = PngCache::default().png(RasterSource::Flattened, &source, 9)?;
        assert_eq!(actual, expected);
        Ok(())
    }
}
