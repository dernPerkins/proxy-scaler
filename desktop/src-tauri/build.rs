fn main() {
    // Without this, Cargo's build-script re-run tracking can miss newly
    // added/removed files inside a bundled resource directory (a known
    // Tauri gap) — meaning a `make sidecar` re-run that changes what's
    // under PyInstaller's _internal/ tree could silently leave a stale
    // resources/proxy-scaler-serve/ copy in target/, with no error.
    println!("cargo:rerun-if-changed=resources/proxy-scaler-serve");
    tauri_build::build()
}
