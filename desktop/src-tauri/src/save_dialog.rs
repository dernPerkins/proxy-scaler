//! Keeping a saved file's extension through the native "Save As" dialog.
//!
//! A dialog opened with a suggested name but no file-type filter loses the
//! extension on Windows: IFileSaveDialog gets no SetFileTypes and no
//! SetDefaultExtension (rfd only sets those from filters), so the type
//! box reads "All files", and a name edited or retyped there is written
//! exactly as typed — "My Deck", not "My Deck.pdf" — a file nothing will
//! open on double-click. macOS never showed it because NSSavePanel keeps
//! the suggested name's extension on its own. The filter fixes Windows;
//! `with_extension_kept` backstops dialogs that never append one (GTK).

use std::path::{Path, PathBuf};

/// The extension of the name offered in the dialog, lower-cased — the
/// type the bytes about to be written actually are. None for a name
/// without one, in which case nothing is filtered or appended.
pub fn expected_extension(suggested_name: &str) -> Option<String> {
    Path::new(suggested_name)
        .extension()
        .and_then(|e| e.to_str())
        .filter(|e| !e.is_empty())
        .map(|e| e.to_ascii_lowercase())
}

/// Label for the dialog's type box. Only the formats this app exports get
/// a proper name; anything else still gets a working filter.
pub fn filter_label(extension: &str) -> String {
    match extension {
        "pdf" => "PDF document".into(),
        "zip" => "ZIP archive".into(),
        "svg" => "SVG image".into(),
        "png" => "PNG image".into(),
        "jpg" | "jpeg" => "JPEG image".into(),
        "webp" => "WebP image".into(),
        other => format!("{} file", other.to_ascii_uppercase()),
    }
}

/// `chosen` with `.{extension}` appended when it doesn't already end in
/// it. A mismatch appends rather than replaces: "Deck v1.2" is a name
/// with a dot in it, not a file of type "2".
///
/// Left alone when the appended path already exists — the dialog's
/// overwrite prompt was for the name the user typed, and writing over a
/// different file they were never asked about is worse than a missing
/// extension.
pub fn with_extension_kept(chosen: PathBuf, extension: &str) -> PathBuf {
    let matches = chosen
        .extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case(extension));
    if matches {
        return chosen;
    }
    let mut name = chosen.clone().into_os_string();
    name.push(".");
    name.push(extension);
    let extended = PathBuf::from(name);
    if extended.exists() {
        chosen
    } else {
        extended
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expected_extension_is_lowercased() {
        assert_eq!(expected_extension("My Deck.PDF").as_deref(), Some("pdf"));
        assert_eq!(expected_extension("deck-cut.svg").as_deref(), Some("svg"));
        assert_eq!(expected_extension("noext"), None);
    }

    #[test]
    fn keeps_a_matching_extension_in_any_case() {
        let p = PathBuf::from("/nowhere/deck.PDF");
        assert_eq!(with_extension_kept(p.clone(), "pdf"), p);
    }

    #[test]
    fn appends_a_missing_extension() {
        assert_eq!(
            with_extension_kept(PathBuf::from("/nowhere/My Deck"), "pdf"),
            PathBuf::from("/nowhere/My Deck.pdf"),
        );
    }

    #[test]
    fn a_dot_in_the_name_is_not_an_extension() {
        assert_eq!(
            with_extension_kept(PathBuf::from("/nowhere/Deck v1.2"), "pdf"),
            PathBuf::from("/nowhere/Deck v1.2.pdf"),
        );
    }

    #[test]
    fn never_targets_an_existing_file_the_dialog_did_not_confirm() {
        let dir = std::env::temp_dir().join(format!("save-dialog-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("taken.pdf"), b"x").unwrap();
        let chosen = dir.join("taken");
        assert_eq!(with_extension_kept(chosen.clone(), "pdf"), chosen);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
