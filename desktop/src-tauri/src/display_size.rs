//! Every active display's physical size, so the Decklist's Grid view can
//! draw a card at its real 63 × 88 mm at 100% zoom (ADR 0010).
//!
//! CSS's own `mm` assumes 96 dpi, which on a Retina MacBook (≈127 points per
//! inch) draws a card at about 75% of its true size. The real figure needs
//! the display's size in millimetres and its width in points — the unit a
//! CSS px is in the web view at page zoom 1 — and points ÷ mm is CSS px per
//! mm.
//!
//! macOS: CGDisplayScreenSize reads the display's EDID (accurate on Apple
//! panels; an external monitor is only as right as its EDID, and some TVs
//! and projectors report nothing), CGDisplayBounds gives the same display in
//! points. Windows and Linux return nothing yet, and the frontend falls back
//! to the CSS guess plus Calibrate.

use serde::Serialize;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DisplaySize {
    /// Stable across restarts and replugs (vendor, model and serial from the
    /// EDID), unlike the display ID: what a calibration is saved under.
    key: String,
    /// Global bounds in points, origin top-left of the main display — the
    /// same space as the web view's `window.screenX/Y`.
    x: f64,
    y: f64,
    width_pt: f64,
    height_pt: f64,
    /// 0 when the display doesn't report a size.
    width_mm: f64,
    height_mm: f64,
}

#[cfg(target_os = "macos")]
mod cg {
    #[repr(C)]
    #[derive(Clone, Copy)]
    pub struct CGPoint {
        pub x: f64,
        pub y: f64,
    }
    #[repr(C)]
    #[derive(Clone, Copy)]
    pub struct CGSize {
        pub width: f64,
        pub height: f64,
    }
    #[repr(C)]
    #[derive(Clone, Copy)]
    pub struct CGRect {
        pub origin: CGPoint,
        pub size: CGSize,
    }
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        pub fn CGGetActiveDisplayList(max: u32, displays: *mut u32, count: *mut u32) -> i32;
        pub fn CGDisplayBounds(display: u32) -> CGRect;
        pub fn CGDisplayScreenSize(display: u32) -> CGSize;
        pub fn CGDisplayVendorNumber(display: u32) -> u32;
        pub fn CGDisplayModelNumber(display: u32) -> u32;
        pub fn CGDisplaySerialNumber(display: u32) -> u32;
    }
}

#[tauri::command]
pub fn get_display_sizes() -> Vec<DisplaySize> {
    #[cfg(target_os = "macos")]
    // SAFETY: plain CoreGraphics queries on IDs CGGetActiveDisplayList just
    // returned; no pointers are kept past the call.
    unsafe {
        let mut ids = [0u32; 16];
        let mut count = 0u32;
        if cg::CGGetActiveDisplayList(ids.len() as u32, ids.as_mut_ptr(), &mut count) != 0 {
            return vec![];
        }
        ids[..count as usize]
            .iter()
            .map(|&id| {
                let b = cg::CGDisplayBounds(id);
                let mm = cg::CGDisplayScreenSize(id);
                DisplaySize {
                    key: format!(
                        "{:x}-{:x}-{:x}",
                        cg::CGDisplayVendorNumber(id),
                        cg::CGDisplayModelNumber(id),
                        cg::CGDisplaySerialNumber(id)
                    ),
                    x: b.origin.x,
                    y: b.origin.y,
                    width_pt: b.size.width,
                    height_pt: b.size.height,
                    width_mm: mm.width,
                    height_mm: mm.height,
                }
            })
            .collect()
    }
    #[cfg(not(target_os = "macos"))]
    {
        vec![]
    }
}
