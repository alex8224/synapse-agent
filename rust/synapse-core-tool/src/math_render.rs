use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use ratex_layout::{layout, to_display_list, LayoutOptions};
use ratex_parser::parser::parse;
use ratex_render::{render_to_png, RenderOptions};
use ratex_types::color::Color;
use ratex_types::math_style::MathStyle;

const MAX_SOURCE_CHARS: usize = 4_000;
const MAX_IMAGE_WIDTH: u32 = 4_096;
const MAX_IMAGE_HEIGHT: u32 = 2_048;
const MAX_IMAGE_PIXELS: u64 = 8_388_608;
const MAX_PNG_BYTES: usize = 8 * 1024 * 1024;

fn parse_color(value: &str, name: &str) -> PyResult<Color> {
    if !value.starts_with('#') {
        return Err(PyValueError::new_err(format!(
            "{name} must be a hex color in #RGB, #RGBA, #RRGGBB, or #RRGGBBAA form"
        )));
    }
    Color::from_hex(value).ok_or_else(|| {
        PyValueError::new_err(format!(
            "{name} must be a hex color in #RGB, #RGBA, #RRGGBB, or #RRGGBBAA form"
        ))
    })
}

fn validate_finite_range(value: f32, name: &str, min: f32, max: f32) -> PyResult<()> {
    if !value.is_finite() || !(min..=max).contains(&value) {
        return Err(PyValueError::new_err(format!(
            "{name} must be finite and between {min} and {max}"
        )));
    }
    Ok(())
}

#[pyfunction(
    signature = (
        source,
        display=true,
        font_size=32.0,
        color="#e8eaed",
        background=None,
        padding=4.0,
        device_pixel_ratio=1.0
    )
)]
#[allow(clippy::too_many_arguments)] // Python API exposes independent render controls as keywords.
pub(crate) fn render_math_png<'py>(
    py: Python<'py>,
    source: &str,
    display: bool,
    font_size: f32,
    color: &str,
    background: Option<&str>,
    padding: f32,
    device_pixel_ratio: f32,
) -> PyResult<Bound<'py, PyBytes>> {
    let source = source.trim();
    if source.is_empty() {
        return Err(PyValueError::new_err("source must not be empty"));
    }
    if source.chars().count() > MAX_SOURCE_CHARS {
        return Err(PyValueError::new_err(format!(
            "source exceeds the {MAX_SOURCE_CHARS}-character limit"
        )));
    }
    validate_finite_range(font_size, "font_size", 8.0, 128.0)?;
    validate_finite_range(padding, "padding", 0.0, 64.0)?;
    validate_finite_range(device_pixel_ratio, "device_pixel_ratio", 0.5, 4.0)?;

    let foreground = parse_color(color, "color")?;
    let background = match background {
        Some(value) => parse_color(value, "background")?,
        None => Color::new(0.0, 0.0, 0.0, 0.0),
    };
    let ast = parse(source)
        .map_err(|error| PyValueError::new_err(format!("invalid LaTeX formula: {error}")))?;
    let layout_options = LayoutOptions {
        style: if display {
            MathStyle::Display
        } else {
            MathStyle::Text
        },
        color: foreground,
        ..LayoutOptions::default()
    };
    let layout_box = layout(&ast, &layout_options);
    let display_list = to_display_list(&layout_box);

    let em_px = font_size * device_pixel_ratio;
    let pad_px = padding * device_pixel_ratio;
    let width = (display_list.width as f32 * em_px + 2.0 * pad_px)
        .ceil()
        .max(1.0) as u32;
    let height = ((display_list.height + display_list.depth) as f32 * em_px + 2.0 * pad_px)
        .ceil()
        .max(1.0) as u32;
    let pixels = u64::from(width) * u64::from(height);
    if width > MAX_IMAGE_WIDTH || height > MAX_IMAGE_HEIGHT || pixels > MAX_IMAGE_PIXELS {
        return Err(PyValueError::new_err(format!(
            "rendered formula would exceed image limits: {width}x{height}px"
        )));
    }

    let render_options = RenderOptions {
        font_size,
        padding,
        background_color: background,
        font_dir: String::new(),
        device_pixel_ratio,
    };
    let png = render_to_png(&display_list, &render_options)
        .map_err(|error| PyRuntimeError::new_err(format!("failed to render formula: {error}")))?;
    if png.len() > MAX_PNG_BYTES {
        return Err(PyRuntimeError::new_err(format!(
            "rendered PNG exceeds the {MAX_PNG_BYTES}-byte limit"
        )));
    }
    Ok(PyBytes::new(py, &png))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_supported_hex_colors() {
        assert!(parse_color("#fff", "color").is_ok());
        assert!(parse_color("#11223344", "color").is_ok());
        assert!(parse_color("red", "color").is_err());
        assert!(parse_color("ffffff", "color").is_err());
    }

    #[test]
    fn rejects_non_finite_values() {
        assert!(validate_finite_range(f32::NAN, "value", 0.0, 1.0).is_err());
        assert!(validate_finite_range(f32::INFINITY, "value", 0.0, 1.0).is_err());
    }
}
