//! Live staging-directory pins used for identity-safe publication.

use std::io;

use cap_std::fs::Dir;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum NameObservation {
    Missing,
    Same,
    Different,
}

/// A writable staging capability whose live handle pins the original object.
#[cfg(unix)]
pub(super) struct StagingPin {
    directory: Dir,
}

#[cfg(windows)]
pub(super) struct StagingPin {
    inner: app_icon_windows_fs::PinnedDirectory,
}

#[cfg(not(any(unix, windows)))]
pub(super) struct StagingPin;

impl StagingPin {
    pub(super) fn observe_name(&self, parent: &Dir, name: &str) -> io::Result<NameObservation> {
        match parent.symlink_metadata(name) {
            Ok(metadata) if !metadata.is_dir() || metadata.is_symlink() => {
                Ok(NameObservation::Different)
            }
            Ok(_) => self.matches_name(parent, name).map(|matches| {
                if matches {
                    NameObservation::Same
                } else {
                    NameObservation::Different
                }
            }),
            Err(source) if source.kind() == io::ErrorKind::NotFound => Ok(NameObservation::Missing),
            Err(source) => Err(source),
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use std::{fs, os::unix::fs::symlink};

    use cap_std::{ambient_authority, fs::Dir};
    use tempfile::tempdir;

    use super::{NameObservation, StagingPin};

    type TestResult<T = ()> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

    #[test]
    fn observes_same_missing_and_different_namespace_entries() -> TestResult {
        let temporary = tempdir()?;
        fs::create_dir(temporary.path().join("staging"))?;
        fs::write(temporary.path().join("ordinary-file"), b"not a directory")?;
        symlink("staging", temporary.path().join("directory-symlink"))?;
        let parent = Dir::open_ambient_dir(temporary.path(), ambient_authority())?;
        let pin = StagingPin::open(&parent, "staging")?;

        assert_eq!(pin.observe_name(&parent, "staging")?, NameObservation::Same);
        assert_eq!(
            pin.observe_name(&parent, "missing")?,
            NameObservation::Missing
        );
        assert_eq!(
            pin.observe_name(&parent, "ordinary-file")?,
            NameObservation::Different
        );
        assert_eq!(
            pin.observe_name(&parent, "directory-symlink")?,
            NameObservation::Different
        );
        Ok(())
    }
}

#[cfg(unix)]
impl StagingPin {
    pub(super) fn open(parent: &Dir, name: &str) -> io::Result<Self> {
        Ok(Self {
            directory: open_directory_no_follow(parent, name)?,
        })
    }

    pub(super) const fn directory(&self) -> &Dir {
        &self.directory
    }

    pub(super) fn matches_name(&self, parent: &Dir, name: &str) -> io::Result<bool> {
        let candidate = open_directory_no_follow(parent, name)?;
        Ok(directory_identity(&self.directory)? == directory_identity(&candidate)?)
    }
}

#[cfg(unix)]
fn open_directory_no_follow(parent: &Dir, name: &str) -> io::Result<Dir> {
    use rustix::fs::{Mode, OFlags, openat};

    let descriptor = openat(
        parent,
        name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    Ok(Dir::from_std_file(std::fs::File::from(descriptor)))
}

#[cfg(unix)]
fn directory_identity(directory: &Dir) -> io::Result<(u64, u64)> {
    use cap_std::fs::MetadataExt;

    let metadata = directory.dir_metadata()?;
    Ok((metadata.dev(), metadata.ino()))
}

#[cfg(windows)]
impl StagingPin {
    pub(super) fn open(parent: &Dir, name: &str) -> io::Result<Self> {
        Ok(Self {
            inner: app_icon_windows_fs::pin_directory(parent, name)?,
        })
    }

    pub(super) fn directory(&self) -> &Dir {
        self.inner.directory()
    }

    pub(super) fn matches_name(&self, parent: &Dir, name: &str) -> io::Result<bool> {
        self.inner.matches_name(parent, name)
    }

    pub(super) const fn windows_pin(&self) -> &app_icon_windows_fs::PinnedDirectory {
        &self.inner
    }
}

#[cfg(not(any(unix, windows)))]
impl StagingPin {
    pub(super) fn open(_parent: &Dir, _name: &str) -> io::Result<Self> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "directory pinning is unavailable on this target",
        ))
    }

    pub(super) fn directory(&self) -> &Dir {
        unreachable!("unsupported targets reject generation before staging")
    }

    pub(super) fn matches_name(&self, _parent: &Dir, _name: &str) -> io::Result<bool> {
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "directory pinning is unavailable on this target",
        ))
    }
}
