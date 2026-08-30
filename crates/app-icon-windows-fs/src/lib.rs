//! Audited Windows filesystem primitives for atomic icon-set publication.

#![cfg(windows)]

use std::{
    error::Error,
    fmt, io,
    mem::{align_of, offset_of, size_of},
    os::windows::io::AsRawHandle,
};

use cap_std::fs::{Dir, MetadataExt, OpenOptions, OpenOptionsExt};
use windows_sys::Win32::{
    Foundation::{
        ERROR_ALREADY_EXISTS, ERROR_FILE_EXISTS, ERROR_INVALID_FUNCTION, ERROR_INVALID_PARAMETER,
        ERROR_NOT_SUPPORTED, HANDLE,
    },
    Storage::FileSystem::{
        DELETE, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES, FILE_RENAME_INFO, FILE_RENAME_INFO_0,
        FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, FileRenameInfo, SYNCHRONIZE,
        SetFileInformationByHandle,
    },
};

const MAX_FILE_NAME_UNITS: usize = 255;
const FILE_RENAME_INFO_TAIL_UNITS: usize = (size_of::<FILE_RENAME_INFO>()
    - offset_of!(FILE_RENAME_INFO, FileName))
.div_ceil(size_of::<u16>());
const FILE_NAME_BUFFER_UNITS: usize = MAX_FILE_NAME_UNITS + FILE_RENAME_INFO_TAIL_UNITS;

/// A classified failure from the Windows no-replace rename primitive.
#[derive(Debug)]
pub enum RenameError {
    /// The destination already names a filesystem object.
    AlreadyExists,
    /// The filesystem or protocol does not support handle-relative rename.
    Unsupported(io::Error),
    /// The source, input, permission, or operating-system operation failed.
    Other(io::Error),
}

impl fmt::Display for RenameError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::AlreadyExists => formatter.write_str("the destination already exists"),
            Self::Unsupported(source) => {
                write!(formatter, "handle-relative rename is unsupported: {source}")
            }
            Self::Other(source) => write!(formatter, "Windows rename failed: {source}"),
        }
    }
}

impl Error for RenameError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::AlreadyExists => None,
            Self::Unsupported(source) | Self::Other(source) => Some(source),
        }
    }
}

/// Atomically renames a child directory without replacing any destination.
///
/// Both names must be single ordinary path components below `parent`. The
/// source is opened relative to that capability. Windows resolves the simple
/// destination name in the opened source's current directory, so no ambient
/// absolute path or process current directory participates in publication.
pub fn rename_directory_no_replace(
    parent: &Dir,
    source_name: &str,
    destination_name: &str,
) -> Result<(), RenameError> {
    validate_component(source_name)?;
    let destination = encode_component(destination_name)?;
    let source = open_source_directory(parent, source_name)?;
    let mut rename = RenameInfoBuffer::new(&destination)?;
    let rename_size = rename.byte_len()?;

    call_set_file_information(&source, &mut rename, rename_size).map_err(classify_rename_error)
}

fn validate_component(component: &str) -> Result<(), RenameError> {
    encode_component(component).map(|_| ())
}

fn encode_component(component: &str) -> Result<Vec<u16>, RenameError> {
    let invalid = component.is_empty()
        || matches!(component, "." | "..")
        || component.ends_with([' ', '.'])
        || component.chars().any(|character| {
            character.is_control()
                || matches!(
                    character,
                    '\0' | '/' | '\\' | ':' | '<' | '>' | '"' | '|' | '?' | '*'
                )
        })
        || is_windows_device_name(component);
    if invalid {
        return Err(invalid_input(
            "name must be one ordinary Windows path component",
        ));
    }

    let encoded = component.encode_utf16().collect::<Vec<_>>();
    if encoded.len() > MAX_FILE_NAME_UNITS {
        return Err(invalid_input("name exceeds 255 UTF-16 code units"));
    }
    Ok(encoded)
}

fn is_windows_device_name(component: &str) -> bool {
    let stem = component
        .split('.')
        .next()
        .map(str::trim_end)
        .map(str::to_ascii_uppercase)
        .unwrap_or_default();
    matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || stem.strip_prefix("COM").is_some_and(is_device_suffix)
        || stem.strip_prefix("LPT").is_some_and(is_device_suffix)
}

fn is_device_suffix(suffix: &str) -> bool {
    matches!(
        suffix,
        "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
    )
}

fn open_source_directory(
    parent: &Dir,
    source_name: &str,
) -> Result<cap_std::fs::File, RenameError> {
    let mut options = OpenOptions::new();
    options
        .access_mode(DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT);

    let source = parent
        .open_with(source_name, &options)
        .map_err(RenameError::Other)?;
    let attributes = source
        .metadata()
        .map_err(RenameError::Other)?
        .file_attributes();
    if attributes & FILE_ATTRIBUTE_DIRECTORY == 0 {
        return Err(invalid_input("source is not a directory"));
    }
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(invalid_input("source is a reparse point"));
    }
    Ok(source)
}

fn invalid_input(message: &'static str) -> RenameError {
    RenameError::Other(io::Error::new(io::ErrorKind::InvalidInput, message))
}

#[repr(C)]
struct RenameInfoBuffer {
    anonymous: FILE_RENAME_INFO_0,
    #[cfg(target_pointer_width = "64")]
    alignment_padding: u32,
    root_directory: HANDLE,
    file_name_length: u32,
    file_name: [u16; FILE_NAME_BUFFER_UNITS],
}

impl RenameInfoBuffer {
    fn new(name: &[u16]) -> Result<Self, RenameError> {
        if name.len() > MAX_FILE_NAME_UNITS {
            return Err(invalid_input("name exceeds 255 UTF-16 code units"));
        }
        let byte_length = name
            .len()
            .checked_mul(size_of::<u16>())
            .and_then(|length| u32::try_from(length).ok())
            .ok_or_else(|| invalid_input("encoded name length overflowed"))?;
        let mut file_name = [0_u16; FILE_NAME_BUFFER_UNITS];
        file_name[..name.len()].copy_from_slice(name);

        Ok(Self {
            anonymous: FILE_RENAME_INFO_0 { Flags: 0 },
            #[cfg(target_pointer_width = "64")]
            alignment_padding: 0,
            // A null root plus a simple name is the documented same-directory
            // rename form. The source handle establishes that directory.
            root_directory: std::ptr::null_mut(),
            file_name_length: byte_length,
            file_name,
        })
    }

    fn byte_len(&self) -> Result<u32, RenameError> {
        // SetFileInformationByHandle requires a full fixed header followed by
        // FileNameLength bytes, including the header's tail padding.
        size_of::<FILE_RENAME_INFO>()
            .checked_add(
                usize::try_from(self.file_name_length)
                    .map_err(|_| invalid_input("encoded name length is not representable"))?,
            )
            .and_then(|length| u32::try_from(length).ok())
            .ok_or_else(|| invalid_input("rename information length overflowed"))
    }
}

const _: () = {
    assert!(align_of::<RenameInfoBuffer>() >= align_of::<FILE_RENAME_INFO>());
    assert!(offset_of!(RenameInfoBuffer, anonymous) == offset_of!(FILE_RENAME_INFO, Anonymous));
    assert!(
        offset_of!(RenameInfoBuffer, root_directory) == offset_of!(FILE_RENAME_INFO, RootDirectory)
    );
    assert!(
        offset_of!(RenameInfoBuffer, file_name_length)
            == offset_of!(FILE_RENAME_INFO, FileNameLength)
    );
    assert!(offset_of!(RenameInfoBuffer, file_name) == offset_of!(FILE_RENAME_INFO, FileName));
    assert!(
        offset_of!(RenameInfoBuffer, file_name) + size_of::<[u16; FILE_NAME_BUFFER_UNITS]>()
            >= size_of::<FILE_RENAME_INFO>() + MAX_FILE_NAME_UNITS * size_of::<u16>()
    );
    assert!(
        size_of::<RenameInfoBuffer>()
            >= size_of::<FILE_RENAME_INFO>() + MAX_FILE_NAME_UNITS * size_of::<u16>()
    );
};

#[allow(unsafe_code)]
fn call_set_file_information(
    source: &cap_std::fs::File,
    rename: &mut RenameInfoBuffer,
    rename_size: u32,
) -> io::Result<()> {
    // SAFETY: `source` owns a live Windows handle with DELETE access. The
    // repr(C) buffer's field offsets are compile-time checked against
    // FILE_RENAME_INFO, `rename_size` covers exactly its initialized prefix,
    // and the simple destination name is stored inside that live buffer.
    let result = unsafe {
        SetFileInformationByHandle(
            source.as_raw_handle().cast(),
            FileRenameInfo,
            std::ptr::from_mut(rename).cast(),
            rename_size,
        )
    };
    if result == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

fn classify_rename_error(source: io::Error) -> RenameError {
    match source.raw_os_error().map(|code| code as u32) {
        Some(ERROR_FILE_EXISTS | ERROR_ALREADY_EXISTS) => RenameError::AlreadyExists,
        Some(ERROR_INVALID_FUNCTION | ERROR_NOT_SUPPORTED | ERROR_INVALID_PARAMETER) => {
            RenameError::Unsupported(source)
        }
        _ => RenameError::Other(source),
    }
}

#[cfg(test)]
mod tests {
    use std::{
        fs, io,
        mem::{offset_of, size_of},
        sync::Arc,
        thread,
    };

    use cap_std::{ambient_authority, fs::Dir};
    use tempfile::tempdir;

    use super::{
        FILE_NAME_BUFFER_UNITS, FILE_RENAME_INFO, FILE_RENAME_INFO_TAIL_UNITS, MAX_FILE_NAME_UNITS,
        RenameError, RenameInfoBuffer, encode_component, rename_directory_no_replace,
    };

    type TestResult<T = ()> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

    #[test]
    fn rename_buffer_covers_the_documented_header_and_name_length() -> TestResult {
        let name = encode_component("published")?;
        let rename = RenameInfoBuffer::new(&name)?;
        let expected_length = size_of::<FILE_RENAME_INFO>() + name.len() * size_of::<u16>();

        assert_eq!(usize::try_from(rename.byte_len()?)?, expected_length);
        assert!(rename.root_directory.is_null());
        assert_eq!(&rename.file_name[..name.len()], name);
        assert!(
            rename.file_name[name.len()..name.len() + FILE_RENAME_INFO_TAIL_UNITS]
                .iter()
                .all(|unit| *unit == 0)
        );

        let maximum_name = vec![u16::from(b'a'); MAX_FILE_NAME_UNITS];
        let maximum = RenameInfoBuffer::new(&maximum_name)?;
        let maximum_length = usize::try_from(maximum.byte_len()?)?;
        assert!(
            maximum_length
                <= offset_of!(RenameInfoBuffer, file_name)
                    + size_of::<[u16; FILE_NAME_BUFFER_UNITS]>()
        );
        assert!(maximum_length <= size_of::<RenameInfoBuffer>());
        Ok(())
    }

    #[test]
    fn renames_relative_to_parent_without_replacement() -> TestResult {
        let temporary = tempdir()?;
        fs::create_dir(temporary.path().join("staging"))?;
        fs::write(temporary.path().join("staging/sentinel"), b"complete")?;
        let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;

        rename_directory_no_replace(&parent, "staging", "published")?;

        assert!(!temporary.path().join("staging").exists());
        assert_eq!(
            fs::read(temporary.path().join("published/sentinel"))?,
            b"complete"
        );
        Ok(())
    }

    #[test]
    fn preserves_existing_file_and_directory_destinations() -> TestResult {
        for destination_is_directory in [false, true] {
            let temporary = tempdir()?;
            fs::create_dir(temporary.path().join("staging"))?;
            fs::write(temporary.path().join("staging/source"), b"source")?;
            let destination = temporary.path().join("published");
            if destination_is_directory {
                fs::create_dir(&destination)?;
                fs::write(destination.join("sentinel"), b"directory")?;
            } else {
                fs::write(&destination, b"file")?;
            }
            let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;

            let error = rename_directory_no_replace(&parent, "staging", "published")
                .err()
                .ok_or_else(|| io::Error::other("existing destination was replaced"))?;

            assert!(matches!(error, RenameError::AlreadyExists));
            assert!(temporary.path().join("staging/source").is_file());
            if destination_is_directory {
                assert_eq!(fs::read(destination.join("sentinel"))?, b"directory");
            } else {
                assert_eq!(fs::read(destination)?, b"file");
            }
        }
        Ok(())
    }

    #[test]
    fn concurrent_publish_has_exactly_one_winner() -> TestResult {
        const COMPETITORS: usize = 16;
        let temporary = tempdir()?;
        for index in 0..COMPETITORS {
            let staging = temporary.path().join(format!("staging-{index}"));
            fs::create_dir(&staging)?;
            fs::write(staging.join("winner"), index.to_string())?;
        }
        let barrier = Arc::new(std::sync::Barrier::new(COMPETITORS + 1));
        let mut workers = Vec::new();
        for index in 0..COMPETITORS {
            let parent_path = temporary.path().to_path_buf();
            let worker_barrier = Arc::clone(&barrier);
            workers.push(thread::spawn(move || -> Result<(), RenameError> {
                let parent = Dir::open_ambient_dir(&parent_path, ambient_authority())
                    .map_err(RenameError::Other)?;
                worker_barrier.wait();
                rename_directory_no_replace(&parent, &format!("staging-{index}"), "published")
            }));
        }
        barrier.wait();

        let mut winners = 0;
        let mut collisions = 0;
        for worker in workers {
            match worker
                .join()
                .map_err(|_| io::Error::other("rename worker panicked"))?
            {
                Ok(()) => winners += 1,
                Err(RenameError::AlreadyExists) => collisions += 1,
                Err(error) => return Err(error.into()),
            }
        }
        assert_eq!(winners, 1);
        assert_eq!(collisions, COMPETITORS - 1);
        assert!(temporary.path().join("published/winner").is_file());
        Ok(())
    }

    #[test]
    fn validates_components_and_utf16_boundaries() -> TestResult {
        for invalid in [
            "",
            ".",
            "..",
            "nested/path",
            "nested\\path",
            "stream:data",
            "COM0",
            "com¹.preview",
            "LPT²",
            "NUL .assets",
            "COM1 .preview",
            "bad?.name",
            "line\nbreak",
        ] {
            assert!(encode_component(invalid).is_err(), "accepted `{invalid}`");
        }
        assert_eq!(
            encode_component("图标-🚀")?,
            "图标-🚀".encode_utf16().collect::<Vec<_>>()
        );
        assert!(encode_component(&"a".repeat(255)).is_ok());
        assert!(encode_component(&"a".repeat(256)).is_err());
        Ok(())
    }
}
