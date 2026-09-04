use std::path::Path;

use serde::Serialize;

use crate::DomainError;

const MAX_RELATIVE_PATH_BYTES: usize = 1_024;
const MAX_COMPONENT_BYTES: usize = 255;

/// A portable, UTF-8, workspace-relative path.
///
/// Only forward-slash-separated normal components are accepted. The value is
/// safe to pass to a capability directory without permitting lexical escape.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize)]
#[serde(transparent)]
pub struct RelativePath(String);

impl RelativePath {
    /// Validates and constructs a relative path.
    pub fn new(value: impl Into<String>) -> Result<Self, DomainError> {
        let value = value.into();
        validate_relative_path(&value)?;
        Ok(Self(value))
    }

    /// Returns the normalized textual representation.
    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Returns the value as a platform path.
    #[must_use]
    pub fn as_path(&self) -> &Path {
        Path::new(&self.0)
    }

    /// Joins a validated normal component to this path.
    pub fn join_component(&self, component: &str) -> Result<Self, DomainError> {
        validate_component(component, &self.0)?;
        Self::new(format!("{}/{}", self.0, component))
    }
}

impl std::fmt::Display for RelativePath {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

fn validate_relative_path(value: &str) -> Result<(), DomainError> {
    if value.is_empty() {
        return Err(invalid(value, "path must not be empty"));
    }
    if value.len() > MAX_RELATIVE_PATH_BYTES {
        return Err(invalid(value, "path is too long"));
    }
    if value.starts_with('/') || value.starts_with('\\') {
        return Err(invalid(value, "absolute paths are not allowed"));
    }
    if value.contains('\\') {
        return Err(invalid(value, "backslash separators are not allowed"));
    }
    if value.contains(':') {
        return Err(invalid(value, "colon is not portable in path components"));
    }
    if value
        .chars()
        .any(|character| matches!(character, '<' | '>' | '"' | '|' | '?' | '*'))
    {
        return Err(invalid(
            value,
            "Windows-reserved path characters are not allowed",
        ));
    }
    if value.chars().any(char::is_control) {
        return Err(invalid(value, "control characters are not allowed"));
    }

    for component in value.split('/') {
        validate_component(component, value)?;
    }

    Ok(())
}

fn validate_component(component: &str, full_value: &str) -> Result<(), DomainError> {
    if component.is_empty() {
        return Err(invalid(full_value, "empty path components are not allowed"));
    }
    if matches!(component, "." | "..") {
        return Err(invalid(full_value, "dot path components are not allowed"));
    }
    if component.len() > MAX_COMPONENT_BYTES {
        return Err(invalid(full_value, "path component is too long"));
    }
    if component.contains('/') || component.contains('\\') {
        return Err(invalid(
            full_value,
            "path separators are not allowed inside a component",
        ));
    }
    if component.ends_with(' ') || component.ends_with('.') {
        return Err(invalid(
            full_value,
            "components must not end with a space or dot",
        ));
    }
    if is_windows_device_name(component) {
        return Err(invalid(full_value, "Windows device names are not allowed"));
    }
    Ok(())
}

pub(crate) fn is_windows_device_name(component: &str) -> bool {
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

fn invalid(value: &str, reason: &'static str) -> DomainError {
    DomainError::InvalidRelativePath {
        value: value.to_owned(),
        reason,
    }
}

#[cfg(test)]
mod tests {
    use super::RelativePath;

    #[test]
    fn accepts_normal_relative_paths() {
        let path = RelativePath::new("assets/icons/app.png");
        assert!(path.is_ok());
    }

    #[test]
    fn rejects_traversal_and_portability_hazards() {
        for value in [
            "../icon.png",
            "assets/../icon.png",
            "/tmp/icon.png",
            "C:\\icon.png",
            "icons//app.png",
            "icons/NUL.png",
            "icons/conin$",
            "icons/CONOUT$.png",
            "icons/CONIN$ .preview",
            "icons/com0.png",
            "icons/COM¹.preview.png",
            "icons/lpt²",
            "icons/NUL .assets",
            "icons/com1 .preview",
            "icons/app. ",
            "icons/app?.png",
            "icons/app|preview.png",
        ] {
            assert!(RelativePath::new(value).is_err(), "accepted `{value}`");
        }
    }

    #[test]
    fn join_component_rejects_nested_paths() {
        let parent = match RelativePath::new("icons") {
            Ok(path) => path,
            Err(error) => panic!("test fixture path failed: {error}"),
        };

        assert!(parent.join_component("nested/app.png").is_err());
    }
}
