//! Pure publication-outcome reconciliation state machine.

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum EntryState {
    Missing,
    Expected,
    Different,
    Unobservable(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(super) enum NativeResult {
    Succeeded,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Resolution {
    Published,
    NotPublished,
    Indeterminate(String),
}

pub(super) fn resolve(
    native_result: NativeResult,
    staging: &EntryState,
    final_entry: &EntryState,
) -> Resolution {
    if matches!(final_entry, EntryState::Expected) && !matches!(staging, EntryState::Expected) {
        return Resolution::Published;
    }

    if native_result == NativeResult::Failed
        && matches!(staging, EntryState::Expected)
        && matches!(final_entry, EntryState::Missing | EntryState::Different)
    {
        return Resolution::NotPublished;
    }

    Resolution::Indeterminate(format!(
        "native_result={}; staging={}; final={}",
        native_result.description(),
        staging.description(),
        final_entry.description()
    ))
}

impl EntryState {
    pub(super) fn description(&self) -> String {
        match self {
            Self::Missing => "missing".to_owned(),
            Self::Expected => "original staging identity".to_owned(),
            Self::Different => "different filesystem object".to_owned(),
            Self::Unobservable(reason) => format!("unobservable ({reason})"),
        }
    }
}

impl NativeResult {
    const fn description(self) -> &'static str {
        match self {
            Self::Succeeded => "success",
            Self::Failed => "error",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{EntryState, NativeResult, Resolution, resolve};

    #[test]
    fn proves_publication_only_from_the_original_final_identity() {
        for native in [NativeResult::Succeeded, NativeResult::Failed] {
            for staging in [
                EntryState::Missing,
                EntryState::Different,
                EntryState::Unobservable("permission denied".to_owned()),
            ] {
                assert_eq!(
                    resolve(native, &staging, &EntryState::Expected),
                    Resolution::Published
                );
            }
        }
    }

    #[test]
    fn proves_non_publication_only_after_a_native_error_with_owned_staging() {
        for final_entry in [EntryState::Missing, EntryState::Different] {
            assert_eq!(
                resolve(NativeResult::Failed, &EntryState::Expected, &final_entry),
                Resolution::NotPublished
            );
        }

        assert!(matches!(
            resolve(
                NativeResult::Succeeded,
                &EntryState::Expected,
                &EntryState::Missing
            ),
            Resolution::Indeterminate(_)
        ));
    }

    #[test]
    fn preserves_every_ambiguous_or_invariant_violating_state() {
        let cases = [
            (EntryState::Missing, EntryState::Missing),
            (EntryState::Different, EntryState::Different),
            (EntryState::Expected, EntryState::Expected),
            (
                EntryState::Expected,
                EntryState::Unobservable("access denied".to_owned()),
            ),
            (
                EntryState::Unobservable("access denied".to_owned()),
                EntryState::Missing,
            ),
        ];

        for native in [NativeResult::Succeeded, NativeResult::Failed] {
            for (staging, final_entry) in &cases {
                assert!(matches!(
                    resolve(native, staging, final_entry),
                    Resolution::Indeterminate(_)
                ));
            }
        }
    }
}
