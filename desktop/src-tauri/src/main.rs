// Phase 0 spike: no custom commands, no IPC — the window just points at a
// manually-started Streamlit instance (see tauri.conf.json's window `url`).
// The only thing under test here is whether the OS-native webview (the same
// engine any wrapper choice would use — WebView2/WKWebView/WebKitGTK) holds
// a long-lived Streamlit websocket connection through a real GPU job without
// dropping or needing a reconnect.
fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
