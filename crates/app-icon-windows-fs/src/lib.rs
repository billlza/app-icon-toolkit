//! Audited Windows filesystem primitives for atomic icon-set publication.

#![cfg(windows)]

use std::{
    error::Error,
    fmt, io,
    mem::{align_of, offset_of, size_of},
    os::windows::io::AsRawHandle,
};

use cap_std::fs::{Dir, MetadataExt, OpenOptions, OpenOptionsExt};
use windows_sys::Wdk::Storage::FileSystem::{
    FILE_RENAME_INFORMATION, FILE_RENAME_INFORMATION_0, FileRenameInformation, NtSetInformationFile,
};
use windows_sys::Win32::{
    Foundation::{
        ERROR_ALREADY_EXISTS, ERROR_FILE_EXISTS, ERROR_INVALID_FUNCTION, ERROR_INVALID_PARAMETER,
        ERROR_NOT_SUPPORTED, HANDLE, RtlNtStatusToDosError, STATUS_SUCCESS,
    },
    Storage::FileSystem::{
        DELETE, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_GENERIC_READ, FILE_ID_INFO, FILE_READ_ATTRIBUTES,
        FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, FileIdInfo,
        GetFileInformationByHandleEx, SYNCHRONIZE,
    },
    System::IO::{IO_STATUS_BLOCK, IO_STATUS_BLOCK_0},
};

const MAX_FILE_NAME_UNITS: usize = 255;
const FILE_RENAME_INFO_TAIL_UNITS: usize = (size_of::<FILE_RENAME_INFORMATION>()
    - offset_of!(FILE_RENAME_INFORMATION, FileName))
.div_ceil(size_of::<u16>());
const FILE_NAME_BUFFER_UNITS: usize = MAX_FILE_NAME_UNITS + FILE_RENAME_INFO_TAIL_UNITS;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct DirectoryIdentity {
    volume_serial_number: u64,
    file_id: [u8; 16],
}

/// Opens staging and keeps the same writable, rename-capable handle live.
///
/// The handle remains live through validation, rename, and reconciliation. Its
/// sharing mode prevents the source link from being deleted or renamed, and
/// the handle itself is used for the native rename.
pub struct PinnedDirectory {
    directory: Dir,
}

impl PinnedDirectory {
    /// Returns the capability used for child artifact I/O.
    #[must_use]
    pub fn directory(&self) -> &Dir {
        &self.directory
    }

    /// Compares the pinned object with the object currently named below `parent`.
    pub fn matches_name(&self, parent: &Dir, name: &str) -> io::Result<bool> {
        validate_component(name).map_err(rename_error_into_io)?;
        let candidate = open_identity_directory(parent, name)?;
        Ok(identity_from_handle(&self.directory)? == identity_from_handle(&candidate)?)
    }
}

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
/// The source is the already pinned handle used for artifact I/O. The
/// destination must be one ordinary path component below `parent` and remains
/// relative to that parent handle throughout the native operation. No ambient
/// absolute path or process current directory participates.
pub fn rename_directory_no_replace(
    parent: &Dir,
    destination_name: &str,
    source: &PinnedDirectory,
) -> Result<(), RenameError> {
    let destination = encode_component(destination_name)?;
    let mut rename = RenameInfoBuffer::new(parent, &destination)?;
    let rename_size = rename.byte_len()?;

    call_nt_set_information(parent, &source.directory, &mut rename, rename_size)
        .map_err(classify_rename_error)
}

/// Opens a non-reparse child directory and keeps its rename-capable handle live.
pub fn pin_directory(parent: &Dir, name: &str) -> io::Result<PinnedDirectory> {
    validate_component(name).map_err(rename_error_into_io)?;
    let source = open_pinned_directory(parent, name)?;
    Ok(PinnedDirectory {
        directory: Dir::from_std_file(source.into_std()),
    })
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
    matches!(
        stem.as_str(),
        "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
    ) || stem.strip_prefix("COM").is_some_and(is_device_suffix)
        || stem.strip_prefix("LPT").is_some_and(is_device_suffix)
}

fn is_device_suffix(suffix: &str) -> bool {
    matches!(
        suffix,
        "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
    )
}

fn open_pinned_directory(parent: &Dir, source_name: &str) -> io::Result<cap_std::fs::File> {
    let mut options = OpenOptions::new();
    options
        .access_mode(FILE_GENERIC_READ | DELETE | SYNCHRONIZE)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT);

    let source = parent.open_with(source_name, &options)?;
    validate_directory_attributes(source.metadata()?.file_attributes())?;
    Ok(source)
}

fn open_identity_directory(parent: &Dir, source_name: &str) -> io::Result<cap_std::fs::File> {
    let mut options = OpenOptions::new();
    options
        .access_mode(FILE_READ_ATTRIBUTES | SYNCHRONIZE)
        .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT);

    let source = parent.open_with(source_name, &options)?;
    validate_directory_attributes(source.metadata()?.file_attributes())?;
    Ok(source)
}

fn validate_directory_attributes(attributes: u32) -> io::Result<()> {
    if attributes & FILE_ATTRIBUTE_DIRECTORY == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "source is not a directory",
        ));
    }
    if attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "source is a reparse point",
        ));
    }
    Ok(())
}

#[allow(unsafe_code)]
fn identity_from_handle(handle: &impl AsRawHandle) -> io::Result<DirectoryIdentity> {
    let mut information = FILE_ID_INFO::default();
    // SAFETY: `handle` refers to a live handle opened with FILE_READ_ATTRIBUTES;
    // `information` is a correctly sized writable FILE_ID_INFO buffer for the
    // requested FileIdInfo class and remains live for the duration of the call.
    let succeeded = unsafe {
        GetFileInformationByHandleEx(
            handle.as_raw_handle().cast(),
            FileIdInfo,
            std::ptr::from_mut(&mut information).cast(),
            u32::try_from(size_of::<FILE_ID_INFO>())
                .map_err(|_| io::Error::other("FILE_ID_INFO size is not representable"))?,
        )
    };
    if succeeded == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(DirectoryIdentity {
        volume_serial_number: information.VolumeSerialNumber,
        file_id: information.FileId.Identifier,
    })
}

fn invalid_input(message: &'static str) -> RenameError {
    RenameError::Other(io::Error::new(io::ErrorKind::InvalidInput, message))
}

fn rename_error_into_io(error: RenameError) -> io::Error {
    match error {
        RenameError::AlreadyExists => io::Error::new(
            io::ErrorKind::AlreadyExists,
            "the destination already exists",
        ),
        RenameError::Unsupported(source) | RenameError::Other(source) => source,
    }
}

#[repr(C)]
struct RenameInfoBuffer {
    anonymous: FILE_RENAME_INFORMATION_0,
    #[cfg(target_pointer_width = "64")]
    alignment_padding: u32,
    root_directory: HANDLE,
    file_name_length: u32,
    file_name: [u16; FILE_NAME_BUFFER_UNITS],
}

impl RenameInfoBuffer {
    fn new(parent: &Dir, name: &[u16]) -> Result<Self, RenameError> {
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
            anonymous: FILE_RENAME_INFORMATION_0 { Flags: 0 },
            #[cfg(target_pointer_width = "64")]
            alignment_padding: 0,
            root_directory: parent.as_raw_handle().cast(),
            file_name_length: byte_length,
            file_name,
        })
    }

    fn byte_len(&self) -> Result<u32, RenameError> {
        // NtSetInformationFile requires a full fixed header followed by
        // FileNameLength bytes, including the header's tail padding.
        size_of::<FILE_RENAME_INFORMATION>()
            .checked_add(
                usize::try_from(self.file_name_length)
                    .map_err(|_| invalid_input("encoded name length is not representable"))?,
            )
            .and_then(|length| u32::try_from(length).ok())
            .ok_or_else(|| invalid_input("rename information length overflowed"))
    }
}

const _: () = {
    assert!(align_of::<RenameInfoBuffer>() >= align_of::<FILE_RENAME_INFORMATION>());
    assert!(
        offset_of!(RenameInfoBuffer, anonymous) == offset_of!(FILE_RENAME_INFORMATION, Anonymous)
    );
    assert!(
        offset_of!(RenameInfoBuffer, root_directory)
            == offset_of!(FILE_RENAME_INFORMATION, RootDirectory)
    );
    assert!(
        offset_of!(RenameInfoBuffer, file_name_length)
            == offset_of!(FILE_RENAME_INFORMATION, FileNameLength)
    );
    assert!(
        offset_of!(RenameInfoBuffer, file_name) == offset_of!(FILE_RENAME_INFORMATION, FileName)
    );
    assert!(
        offset_of!(RenameInfoBuffer, file_name) + size_of::<[u16; FILE_NAME_BUFFER_UNITS]>()
            >= size_of::<FILE_RENAME_INFORMATION>() + MAX_FILE_NAME_UNITS * size_of::<u16>()
    );
    assert!(
        size_of::<RenameInfoBuffer>()
            >= size_of::<FILE_RENAME_INFORMATION>() + MAX_FILE_NAME_UNITS * size_of::<u16>()
    );
};

#[allow(unsafe_code)]
fn call_nt_set_information(
    parent: &Dir,
    source: &Dir,
    rename: &mut RenameInfoBuffer,
    rename_size: u32,
) -> io::Result<()> {
    debug_assert_eq!(
        rename.root_directory,
        parent.as_raw_handle().cast::<std::ffi::c_void>()
    );
    let mut io_status = IO_STATUS_BLOCK {
        Anonymous: IO_STATUS_BLOCK_0 {
            Status: STATUS_SUCCESS,
        },
        Information: 0,
    };
    // SAFETY: `source` owns a live Windows handle with DELETE access. The
    // repr(C) buffer's field offsets are compile-time checked against
    // FILE_RENAME_INFORMATION, `rename_size` covers exactly its initialized
    // prefix, and `parent` keeps the RootDirectory handle live for the call.
    let status = unsafe {
        NtSetInformationFile(
            source.as_raw_handle().cast(),
            &mut io_status,
            std::ptr::from_mut(rename).cast(),
            rename_size,
            FileRenameInformation,
        )
    };
    if status == STATUS_SUCCESS {
        Ok(())
    } else {
        // SAFETY: this is the documented ntdll mapping for an NTSTATUS value.
        let error_code = unsafe { RtlNtStatusToDosError(status) };
        Err(io::Error::from_raw_os_error(error_code as i32))
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
        os::windows::io::AsRawHandle,
        sync::Arc,
        thread,
    };

    use cap_std::{ambient_authority, fs::Dir};
    use tempfile::tempdir;

    use super::{
        FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_NAME_BUFFER_UNITS,
        FILE_RENAME_INFO_TAIL_UNITS, FILE_RENAME_INFORMATION, MAX_FILE_NAME_UNITS, RenameError,
        RenameInfoBuffer, encode_component, pin_directory, rename_directory_no_replace,
        validate_directory_attributes,
    };

    type TestResult<T = ()> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

    #[test]
    fn rename_buffer_covers_the_documented_header_and_name_length() -> TestResult {
        let name = encode_component("published")?;
        let temporary = tempdir()?;
        let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;
        let rename = RenameInfoBuffer::new(&parent, &name)?;
        let expected_length = size_of::<FILE_RENAME_INFORMATION>() + name.len() * size_of::<u16>();

        assert_eq!(usize::try_from(rename.byte_len()?)?, expected_length);
        assert_eq!(
            rename.root_directory,
            parent.as_raw_handle().cast::<std::ffi::c_void>()
        );
        assert_eq!(&rename.file_name[..name.len()], name);
        assert!(
            rename.file_name[name.len()..name.len() + FILE_RENAME_INFO_TAIL_UNITS]
                .iter()
                .all(|unit| *unit == 0)
        );

        let maximum_name = vec![u16::from(b'a'); MAX_FILE_NAME_UNITS];
        let maximum = RenameInfoBuffer::new(&parent, &maximum_name)?;
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
    fn source_handle_pins_the_staging_link_inside_the_capability() -> TestResult {
        let temporary = tempdir()?;
        let scoped_path = temporary.path().join("scope");
        let outside_path = temporary.path().join("outside");
        fs::create_dir(&scoped_path)?;
        fs::create_dir(&outside_path)?;
        fs::create_dir(scoped_path.join("staging"))?;
        let parent = Dir::open_ambient_dir(&scoped_path, ambient_authority())?;
        let source = pin_directory(&parent, "staging")?;

        let move_while_open = fs::rename(scoped_path.join("staging"), outside_path.join("moved"));
        assert!(move_while_open.is_err());
        assert!(scoped_path.join("staging").is_dir());
        assert!(!outside_path.join("moved").exists());

        drop(source);
        fs::rename(scoped_path.join("staging"), outside_path.join("moved"))?;
        assert!(outside_path.join("moved").is_dir());
        Ok(())
    }

    #[test]
    fn pin_rejects_non_directories_and_reparse_attributes() -> TestResult {
        let temporary = tempdir()?;
        fs::write(temporary.path().join("ordinary-file"), b"not a directory")?;
        let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;

        let file_error = pin_directory(&parent, "ordinary-file")
            .err()
            .ok_or_else(|| io::Error::other("ordinary file was unexpectedly pinned"))?;
        assert_eq!(file_error.kind(), io::ErrorKind::InvalidInput);
        assert!(validate_directory_attributes(0).is_err());
        assert!(
            validate_directory_attributes(FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)
                .is_err()
        );
        validate_directory_attributes(FILE_ATTRIBUTE_DIRECTORY)?;
        Ok(())
    }

    #[test]
    fn renames_relative_to_parent_without_replacement() -> TestResult {
        let temporary = tempdir()?;
        fs::create_dir(temporary.path().join("staging"))?;
        let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;
        let source = pin_directory(&parent, "staging")?;
        source.directory().write("sentinel", b"complete")?;

        rename_directory_no_replace(&parent, "published", &source)?;

        assert!(!temporary.path().join("staging").exists());
        assert!(source.matches_name(&parent, "published")?);
        assert_eq!(
            fs::read(temporary.path().join("published/sentinel"))?,
            b"complete"
        );
        assert!(
            fs::rename(
                temporary.path().join("published"),
                temporary.path().join("moved")
            )
            .is_err()
        );
        drop(source);
        fs::rename(
            temporary.path().join("published"),
            temporary.path().join("moved"),
        )?;
        assert!(temporary.path().join("moved/sentinel").is_file());
        Ok(())
    }

    #[test]
    fn live_pin_distinguishes_another_directory() -> TestResult {
        let temporary = tempdir()?;
        fs::create_dir(temporary.path().join("staging-a"))?;
        fs::create_dir(temporary.path().join("staging-b"))?;
        let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;
        let source = pin_directory(&parent, "staging-a")?;

        assert!(source.matches_name(&parent, "staging-a")?);
        assert!(!source.matches_name(&parent, "staging-b")?);
        assert!(temporary.path().join("staging-a").is_dir());
        assert!(temporary.path().join("staging-b").is_dir());
        Ok(())
    }

    #[test]
    fn pinned_source_cannot_be_replaced_before_publication() -> TestResult {
        let temporary = tempdir()?;
        let staging = temporary.path().join("staging");
        fs::create_dir(&staging)?;
        let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;
        let source = pin_directory(&parent, "staging")?;

        assert!(fs::remove_dir(&staging).is_err());
        assert!(source.matches_name(&parent, "staging")?);
        rename_directory_no_replace(&parent, "published", &source)?;

        assert!(!staging.exists());
        assert!(temporary.path().join("published").is_dir());
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
            let source = pin_directory(&parent, "staging")?;

            let error = rename_directory_no_replace(&parent, "published", &source)
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
                let source_name = format!("staging-{index}");
                let source = pin_directory(&parent, &source_name).map_err(RenameError::Other)?;
                worker_barrier.wait();
                rename_directory_no_replace(&parent, "published", &source)
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
            "CONIN$",
            "conout$.preview",
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
