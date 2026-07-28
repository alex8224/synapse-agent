# Tool Output Transformation

Synapse uses dependency-free, deterministic algorithms adapted from Headroom's public compression strategy. It does not install or run `headroom-ai`, its proxy, model providers, or `litellm`. The original is stored only when a rewrite saves space, in `.coding-agent/tool-outputs.sqlite`, and can be recovered through `read_tool_result` with a `tool-output://...` reference.

## Built-in transformers

- `search`: groups `path:line:content` output and preserves query-relevant and error matches.
- `log`: preserves errors, traceback context, summaries, and head/tail samples.
- `diff`: preserves file/hunk metadata, changed lines, and limited change context.
- `json`: keeps error/query-relevant items plus a structured omission summary.
- `code`: keeps imports and signatures with bounded body snippets.
- `text`: head/tail fallback with error anchors.

All transforms are deterministic. A result is passed through unchanged when it grows, fails, or loses a critical error/diff line. Error-status tool results are never transformed.

## Configuration

```json
{
  "enable_tool_output_transform": true,
  "tool_output_transform_threshold_bytes": 8192,
  "tool_output_disabled_types": ["code"],
  "tool_output_transform_plugins": ["my_package.transforms:my_transformer"],
  "enable_native_tool_output_compression": true
}
```

Environment equivalents:

```text
AGENT_ENABLE_TOOL_OUTPUT_TRANSFORM=true
AGENT_TOOL_OUTPUT_TRANSFORM_THRESHOLD_BYTES=8192
AGENT_TOOL_OUTPUT_DISABLED_TYPES=["code"]
AGENT_TOOL_OUTPUT_TRANSFORM_PLUGINS=["my_package.transforms:my_transformer"]
AGENT_ENABLE_NATIVE_TOOL_OUTPUT_COMPRESSION=true
```

A plugin exports an object (or zero-argument class) with `name`, `content_types`, and `transform(content, context)`. Plugin transformers precede built-ins for the declared content type.

## Native Algorithms

When `synapse_tool_compress_core` is installed, the pipeline automatically prefers its prebuilt Apache-2.0 native search, log, diff, JSON, and AST-code algorithms. The native result still passes Synapse's critical-fact and size checks; originals remain in Synapse SQLite and native CCR is disabled.

The wheel is optional. If it is absent, incompatible with the host, or raises during a transform, Synapse uses the deterministic Python transformer for that content type. Disable native selection explicitly with:

```text
AGENT_ENABLE_NATIVE_TOOL_OUTPUT_COMPRESSION=false
```

During local development, install the wheel built under `rust/synapse-tool-compress-core/dist/` with `uv pip install`. After the wheel is published to the configured package index, Synapse can declare it as a normal optional dependency. No user needs Rust, Cargo, maturin, or a compiler; see [native-compression-core](native-compression-core.md).

## Status, events, retrieval, and metrics

Inspect whether transformation and the optional native wheel are active:

```text
synapse tool-output status
```

Inspect aggregate savings, retrieval cost, retention, and execution-path counts:

```text
synapse tool-output stats
synapse tool-output stats --thread <thread_id>
```

Inspect recent per-output decisions for analysis. Each event includes content type, transformer, `native` / `python_only` / `python_fallback_after_native` / `passthrough` path, byte counts, retrieval bytes, critical retention, and the reversible reference when one was created:

```text
synapse tool-output events
synapse tool-output events --thread <thread_id> --limit 100
```

Events written before this capability display `path=unknown` and remain valid historical records.

Use `read_tool_result` with either exact paging or local targeted retrieval:

```text
read_tool_result(ref="tool-output://...", offset=0, limit=200)
read_tool_result(ref="tool-output://...", query="authentication timeout", context_lines=3)
```

Inspect aggregate metrics:

```text
synapse tool-output stats
synapse tool-output stats --thread <thread_id>
```

The metrics include gross savings, retrieval bytes, effective savings after retrieval, and critical-fact retention. The SQLite event tables retain raw byte counts and timing; source content is not copied into metrics.

## Offline evaluation

Use deterministic fixtures without model credentials:

```text
synapse tool-output eval tests/fixtures/tool_output_eval.json
```

Each case declares source content and mandatory strings. It passes only when all mandatory facts survive and the visible result is smaller than the source.
