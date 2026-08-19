mod file_access;
mod file_tools;
mod llm;
mod math_render;
mod search;

use pyo3::prelude::*;

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(search::grep, module)?)?;
    module.add_function(wrap_pyfunction!(search::glob, module)?)?;
    module.add_function(wrap_pyfunction!(file_tools::read, module)?)?;
    module.add_function(wrap_pyfunction!(file_tools::edit, module)?)?;
    module.add_function(wrap_pyfunction!(file_tools::patch, module)?)?;
    module.add_function(wrap_pyfunction!(math_render::render_math_png, module)?)?;
    llm::register(module)?;
    Ok(())
}
