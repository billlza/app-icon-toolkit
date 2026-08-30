//! Bounded source inspection and decoding.

use std::{io::Cursor, io::Read};

use app_icon_domain::{
    IconSources, MAX_SOURCE_BYTES, MAX_SOURCE_EDGE, MIN_ADAPTIVE_EDGE, MIN_FLATTENED_EDGE,
    RelativePath, SourceInspection,
};
use cap_std::fs::Dir;
use image::{DynamicImage, ImageFormat, ImageReader, Limits, RgbaImage};

use crate::EngineError;

const PNG_SIGNATURE: &[u8; 8] = b"\x89PNG\r\n\x1a\n";
const MAX_DECODE_ALLOCATION: u64 = 256 * 1024 * 1024;

#[derive(Debug, Clone, Copy)]
pub(crate) enum SourceRole {
    Flattened,
    AdaptiveForeground,
    AdaptiveBackground,
    AdaptiveMonochrome,
}

pub(crate) struct DecodedSource {
    pub(crate) image: RgbaImage,
    pub(crate) inspection: SourceInspection,
}

pub(crate) struct PreparedAdaptiveSources {
    pub(crate) foreground: DecodedSource,
    pub(crate) background: DecodedSource,
    pub(crate) monochrome: Option<DecodedSource>,
}

pub(crate) struct PreparedSources {
    pub(crate) flattened: DecodedSource,
    pub(crate) adaptive: Option<PreparedAdaptiveSources>,
}

impl PreparedSources {
    pub(crate) fn inspections(&self) -> Vec<SourceInspection> {
        let mut inspections = Vec::with_capacity(if self.adaptive.is_some() { 4 } else { 1 });
        inspections.push(self.flattened.inspection.clone());
        if let Some(adaptive) = &self.adaptive {
            inspections.push(adaptive.foreground.inspection.clone());
            inspections.push(adaptive.background.inspection.clone());
            if let Some(monochrome) = &adaptive.monochrome {
                inspections.push(monochrome.inspection.clone());
            }
        }
        inspections
    }
}

pub(crate) fn inspect_sources(
    root: &Dir,
    sources: &IconSources,
) -> Result<Vec<SourceInspection>, EngineError> {
    let mut inspections = Vec::with_capacity(if sources.adaptive().is_some() { 4 } else { 1 });
    inspections.push(decode_source(root, sources.flattened(), SourceRole::Flattened)?.inspection);
    if let Some(adaptive) = sources.adaptive() {
        inspections.push(
            decode_source(root, adaptive.foreground(), SourceRole::AdaptiveForeground)?.inspection,
        );
        inspections.push(
            decode_source(root, adaptive.background(), SourceRole::AdaptiveBackground)?.inspection,
        );
        if let Some(monochrome) = adaptive.monochrome() {
            inspections
                .push(decode_source(root, monochrome, SourceRole::AdaptiveMonochrome)?.inspection);
        }
    }
    Ok(inspections)
}

pub(crate) fn prepare_sources(
    root: &Dir,
    sources: &IconSources,
) -> Result<PreparedSources, EngineError> {
    let flattened = decode_source(root, sources.flattened(), SourceRole::Flattened)?;
    let adaptive = match sources.adaptive() {
        Some(adaptive) => Some(PreparedAdaptiveSources {
            foreground: decode_source(root, adaptive.foreground(), SourceRole::AdaptiveForeground)?,
            background: decode_source(root, adaptive.background(), SourceRole::AdaptiveBackground)?,
            monochrome: match adaptive.monochrome() {
                Some(path) => Some(decode_source(root, path, SourceRole::AdaptiveMonochrome)?),
                None => None,
            },
        }),
        None => None,
    };
    Ok(PreparedSources {
        flattened,
        adaptive,
    })
}

pub(crate) fn decode_source(
    root: &Dir,
    path: &RelativePath,
    role: SourceRole,
) -> Result<DecodedSource, EngineError> {
    let bytes = read_bounded_source(root, path)?;
    if !bytes.starts_with(PNG_SIGNATURE) {
        return Err(EngineError::UnsupportedSourceFormat { path: path.clone() });
    }
    if contains_animation_control(&bytes) {
        return Err(EngineError::AnimatedPngUnsupported { path: path.clone() });
    }

    let (width, height) = ImageReader::with_format(Cursor::new(&bytes), ImageFormat::Png)
        .into_dimensions()
        .map_err(|source| EngineError::ImageDecode {
            path: path.clone(),
            source,
        })?;
    validate_dimensions(path, width, height, role)?;

    let mut reader = ImageReader::with_format(Cursor::new(bytes), ImageFormat::Png);
    let mut limits = Limits::default();
    limits.max_image_width = Some(MAX_SOURCE_EDGE);
    limits.max_image_height = Some(MAX_SOURCE_EDGE);
    limits.max_alloc = Some(MAX_DECODE_ALLOCATION);
    reader.limits(limits);
    let decoded = reader.decode().map_err(|source| EngineError::ImageDecode {
        path: path.clone(),
        source,
    })?;
    let has_alpha = decoded.color().has_alpha();
    let image = into_rgba(decoded);
    let opaque = image.pixels().all(|pixel| pixel.0[3] == u8::MAX);

    if matches!(role, SourceRole::AdaptiveBackground) && !opaque {
        return Err(EngineError::AdaptiveBackgroundNotOpaque { path: path.clone() });
    }

    Ok(DecodedSource {
        image,
        inspection: SourceInspection::new(path.clone(), width, height, has_alpha, opaque),
    })
}

fn read_bounded_source(root: &Dir, path: &RelativePath) -> Result<Vec<u8>, EngineError> {
    let metadata =
        root.symlink_metadata(path.as_path())
            .map_err(|source| EngineError::SourceRead {
                path: path.clone(),
                source,
            })?;
    if !metadata.is_file() {
        return Err(EngineError::SourceNotRegular { path: path.clone() });
    }
    enforce_size_limit(path, metadata.len())?;

    let file = root
        .open(path.as_path())
        .map_err(|source| EngineError::SourceRead {
            path: path.clone(),
            source,
        })?;
    let opened_metadata = file.metadata().map_err(|source| EngineError::SourceRead {
        path: path.clone(),
        source,
    })?;
    if !opened_metadata.is_file() {
        return Err(EngineError::SourceNotRegular { path: path.clone() });
    }
    enforce_size_limit(path, opened_metadata.len())?;

    let mut bytes = Vec::new();
    let mut limited = file.take(MAX_SOURCE_BYTES + 1);
    limited
        .read_to_end(&mut bytes)
        .map_err(|source| EngineError::SourceRead {
            path: path.clone(),
            source,
        })?;
    enforce_size_limit(path, bytes.len() as u64)?;
    Ok(bytes)
}

fn enforce_size_limit(path: &RelativePath, actual_bytes: u64) -> Result<(), EngineError> {
    if actual_bytes > MAX_SOURCE_BYTES {
        return Err(EngineError::SourceTooLarge {
            path: path.clone(),
            actual_bytes,
            maximum_bytes: MAX_SOURCE_BYTES,
        });
    }
    Ok(())
}

fn validate_dimensions(
    path: &RelativePath,
    width: u32,
    height: u32,
    role: SourceRole,
) -> Result<(), EngineError> {
    let minimum = match role {
        SourceRole::Flattened => MIN_FLATTENED_EDGE,
        SourceRole::AdaptiveForeground
        | SourceRole::AdaptiveBackground
        | SourceRole::AdaptiveMonochrome => MIN_ADAPTIVE_EDGE,
    };
    let requirement = match role {
        SourceRole::Flattened => "must be square, at least 1024px, and at most 4096px per edge",
        SourceRole::AdaptiveForeground
        | SourceRole::AdaptiveBackground
        | SourceRole::AdaptiveMonochrome => {
            "must be square, at least 432px, and at most 4096px per edge"
        }
    };

    if width != height || width < minimum || width > MAX_SOURCE_EDGE {
        return Err(EngineError::InvalidSourceDimensions {
            path: path.clone(),
            width,
            height,
            requirement,
        });
    }
    Ok(())
}

fn into_rgba(image: DynamicImage) -> RgbaImage {
    image.into_rgba8()
}

fn contains_animation_control(bytes: &[u8]) -> bool {
    let mut offset = PNG_SIGNATURE.len();
    while let Some(header_end) = offset.checked_add(8) {
        if header_end > bytes.len() {
            return false;
        }
        let length_bytes = [
            bytes[offset],
            bytes[offset + 1],
            bytes[offset + 2],
            bytes[offset + 3],
        ];
        let data_length = u32::from_be_bytes(length_bytes) as usize;
        let chunk_type = &bytes[offset + 4..header_end];
        let Some(chunk_end) = header_end
            .checked_add(data_length)
            .and_then(|end| end.checked_add(4))
        else {
            return false;
        };
        if chunk_end > bytes.len() {
            return false;
        }
        if chunk_type == b"acTL" {
            return true;
        }
        if chunk_type == b"IEND" {
            return false;
        }
        offset = chunk_end;
    }
    false
}

#[cfg(test)]
mod tests {
    use super::{PNG_SIGNATURE, contains_animation_control};

    #[test]
    fn animation_detection_reads_chunk_boundaries() {
        let animated = png_with_chunk(*b"acTL", &[0, 0, 0, 1, 0, 0, 0, 0]);
        assert!(contains_animation_control(&animated));

        let hidden_text = png_with_chunk(*b"tEXt", b"ordinary acTL text");
        assert!(!contains_animation_control(&hidden_text));

        let mut truncated = PNG_SIGNATURE.to_vec();
        truncated.extend_from_slice(&20_u32.to_be_bytes());
        truncated.extend_from_slice(b"acTL");
        assert!(!contains_animation_control(&truncated));
    }

    fn png_with_chunk(chunk_type: [u8; 4], data: &[u8]) -> Vec<u8> {
        let mut bytes = PNG_SIGNATURE.to_vec();
        bytes.extend_from_slice(&(data.len() as u32).to_be_bytes());
        bytes.extend_from_slice(&chunk_type);
        bytes.extend_from_slice(data);
        bytes.extend_from_slice(&[0; 4]);
        bytes.extend_from_slice(&0_u32.to_be_bytes());
        bytes.extend_from_slice(b"IEND");
        bytes.extend_from_slice(&[0; 4]);
        bytes
    }
}
