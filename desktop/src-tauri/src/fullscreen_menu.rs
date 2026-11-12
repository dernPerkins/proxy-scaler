//! macOS View menu: "Toggle True Full Screen" (⌘⇧F), beside the standard
//! "Toggle Full Screen".
//!
//! Native macOS full screen (its own Space) always stops below the strip
//! beside the notch — the window server resizes the window there itself,
//! and no NSWindow/delegate hook overrides it (tested 2026-11-11 on a
//! notched MacBook Pro). The only native way to reach the top edge is the
//! older "simple" full screen Tauri exposes as `set_simple_fullscreen`: the
//! menu bar and Dock auto-hide and the window takes the whole screen frame,
//! but it stays on the current desktop instead of getting a Space of its
//! own. So it's a separate, explicitly chosen command, while the standard
//! item and the green window button stay native full screen.
//!
//! The standard item is Tauri's predefined toggleFullScreen: one, left to
//! AppKit: it swaps in its own system item labelled 🌐F, which is what
//! carries the 🌐F shortcut (⌃⌘F works too). Replacing it with our own to
//! show ⌃⌘F instead loses 🌐F entirely, so it stays.
//!
//! While true full screen is on, the frontend is told the notch height (0
//! on a screen without one) so the tab bar can grow to meet the camera
//! housing — see the `true-full-screen` event and `html.notch-full-screen`
//! in styles.css.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

use dispatch2::{DispatchQueue, DispatchTime, MainThreadBound};
use objc2::rc::Retained;
use objc2::runtime::{AnyClass, NSObjectProtocol};
use objc2::MainThreadMarker;
use objc2_app_kit::{NSView, NSWindow};
use serde::Serialize;
use tauri::menu::{Menu, MenuEvent, MenuItem, MenuItemKind};
use tauri::{AppHandle, Emitter, Manager, WebviewWindow, Wry};

const TOGGLE_ID: &str = "view.true_full_screen";
const MAIN_WINDOW: &str = "main";
const EVENT: &str = "true-full-screen";
/// How long the web view's tracking areas stay off (see rearm_webview).
const REARM_DELAY: std::time::Duration = std::time::Duration::from_millis(100);

struct TrueFullScreenMenu {
    item: MenuItem<Wry>,
    /// Tauri can't report simple full screen (`is_fullscreen` only reflects
    /// native), so it's tracked here. Only this module enters or leaves it.
    active: AtomicBool,
    /// The notch height in points, as f64 bits, captured on entry.
    notch_inset: AtomicU64,
    /// Whether the item is currently enabled, so the frequent Resized
    /// events only touch the menu when that actually changes.
    enabled: AtomicBool,
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TrueFullScreen {
    active: bool,
    /// Height of the camera housing in CSS px; 0 when the screen has none.
    notch_inset: f64,
}

/// Tauri's default menu with our item added to its View submenu. Called
/// once from setup.
pub fn install(app: &AppHandle) -> tauri::Result<()> {
    let menu = Menu::default(app)?;
    let view = menu.items()?.into_iter().find_map(|item| match item {
        MenuItemKind::Submenu(sub) if sub.text().ok().as_deref() == Some("View") => Some(sub),
        _ => None,
    });
    if let Some(view) = view {
        // ⌘⇧F (usually Find, which this app has no use for). 🌐F can't be
        // used — AppKit only honours the fn modifier on its own system menu
        // items, and logs and drops it on ours.
        let item =
            MenuItem::with_id(app, TOGGLE_ID, "Toggle True Full Screen", true, Some("Cmd+Shift+F"))?;
        view.append(&item)?;
        app.manage(TrueFullScreenMenu {
            item,
            active: AtomicBool::new(false),
            notch_inset: AtomicU64::new(0f64.to_bits()),
            enabled: AtomicBool::new(true),
        });
        app.on_menu_event(on_menu_event);
    }
    // Tauri's default menu has always had a View submenu on macOS; if a
    // future version drops it, the default menu still goes in unchanged.
    app.set_menu(menu)?;
    Ok(())
}

/// For a webview that (re)loads while true full screen is already on.
#[tauri::command]
pub fn get_true_full_screen(app: AppHandle) -> TrueFullScreen {
    match app.try_state::<TrueFullScreenMenu>() {
        Some(state) => snapshot(&state),
        None => TrueFullScreen { active: false, notch_inset: 0.0 },
    }
}

/// Greys the item out while the window is in native full screen, where
/// simple full screen can't be entered. Called on every main-window Resized
/// event, which tao also fires once a native transition finishes.
pub fn refresh(app: &AppHandle) {
    let Some(state) = app.try_state::<TrueFullScreenMenu>() else { return };
    let Some(window) = app.get_webview_window(MAIN_WINDOW) else { return };
    let enabled = !window.is_fullscreen().unwrap_or(false);
    if state.enabled.swap(enabled, Ordering::SeqCst) != enabled {
        let _ = state.item.set_enabled(enabled);
    }
}

fn on_menu_event(app: &AppHandle, event: MenuEvent) {
    if event.id().as_ref() == TOGGLE_ID {
        toggle(app);
    }
}

fn toggle(app: &AppHandle) {
    let Some(state) = app.try_state::<TrueFullScreenMenu>() else { return };
    let Some(window) = app.get_webview_window(MAIN_WINDOW) else { return };
    let enter = !state.active.load(Ordering::SeqCst);
    if enter && window.is_fullscreen().unwrap_or(false) {
        return;
    }
    if window.set_simple_fullscreen(enter).is_err() {
        return;
    }
    state.active.store(enter, Ordering::SeqCst);
    let inset = if enter { notch_inset(&window) } else { 0.0 };
    state.notch_inset.store(inset.to_bits(), Ordering::SeqCst);
    // Leaving puts the title bar back on the main dispatch queue (tao's
    // set_style_mask_async), so queue behind it there, for both directions.
    let for_rearm = window.clone();
    DispatchQueue::main().exec_async(move || rearm_webview(&for_rearm));
    let _ = window.emit(EVENT, snapshot(&state));
}

/// tao enters and leaves simple full screen by removing and restoring the
/// title bar (.Titled), which makes AppKit swap the window's frame view and
/// re-register every tracking area in the window as if the cursor were
/// outside it. WebKit gets the page's mouse moves only through its own
/// tracking area, which then waits for a mouse-entered that never comes
/// while the cursor stays put — and in true full screen the window covers
/// the whole screen, so it can't come at all. Hover, mouseenter and cursor
/// changes went dead until ⌘Tab (reactivation re-syncs it) or the cursor
/// left the window and came back.
///
/// Taking the web view's tracking areas off for a moment and adding the
/// same ones back makes AppKit register them afresh, which works out that
/// the cursor is already inside. They have to stay off through AppKit's
/// own update pass after the swap — re-adding them in one go, or on the
/// very next turn of the main queue, gets folded into that pass and does
/// nothing (REARM_DELAY). Pinned down 2026-11-11 by driving real mouse
/// moves at the app; also tried and not enough: updateTrackingAreas,
/// refocusing the web view, re-keying the window, toggling
/// acceptsMouseMovedEvents/ignoresMouseEvents, re-adding the web view.
///
/// tao also makes its own view first responder each time, taking keyboard
/// focus off the page, so that's handed back too.
fn rearm_webview(window: &WebviewWindow) {
    let Ok(ptr) = window.ns_window() else { return };
    // SAFETY: as in notch_inset — the window's live NSWindow, and the main
    // dispatch queue runs on the main thread.
    let ns_window = unsafe { &*(ptr as *const NSWindow) };
    let Some(mtm) = MainThreadMarker::new() else { return };
    let Some(content) = ns_window.contentView() else { return };
    let Some(wk_web_view) = AnyClass::get(c"WKWebView") else { return };
    let Some(web_view) = find_view(&content, wk_web_view) else { return };
    ns_window.makeFirstResponder(Some(&web_view));
    let areas = web_view.trackingAreas();
    for area in &areas {
        web_view.removeTrackingArea(&area);
    }
    let pending = MainThreadBound::new((web_view, areas), mtm);
    let later = DispatchTime::try_from(REARM_DELAY).unwrap_or(DispatchTime::NOW);
    let _ = DispatchQueue::main().after(later, move || {
        let Some(mtm) = MainThreadMarker::new() else { return };
        let (web_view, areas) = pending.into_inner(mtm);
        for area in &areas {
            web_view.addTrackingArea(&area);
        }
    });
}

/// The first view under `view` (depth first) that is a `class`.
fn find_view(view: &NSView, class: &AnyClass) -> Option<Retained<NSView>> {
    view.subviews().into_iter().find_map(|sub| {
        if sub.isKindOfClass(class) {
            Some(sub)
        } else {
            find_view(&sub, class)
        }
    })
}

fn snapshot(state: &TrueFullScreenMenu) -> TrueFullScreen {
    TrueFullScreen {
        active: state.active.load(Ordering::SeqCst),
        notch_inset: f64::from_bits(state.notch_inset.load(Ordering::SeqCst)),
    }
}

/// `NSScreen.safeAreaInsets.top` for the window's screen: the camera
/// housing's height in points (= CSS px), 0 on screens without one.
/// Menu events arrive on the main thread, which AppKit requires here.
fn notch_inset(window: &WebviewWindow) -> f64 {
    let Ok(ptr) = window.ns_window() else { return 0.0 };
    // SAFETY: Tauri hands back the window's live NSWindow, and this runs
    // on the main thread (menu event handlers are dispatched there).
    let ns_window = unsafe { &*(ptr as *const NSWindow) };
    ns_window.screen().map(|s| s.safeAreaInsets().top).unwrap_or(0.0)
}
