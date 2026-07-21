"""coding-agent package."""

__version__ = "0.1.0"


def main() -> None:
    """Lazy entrypoint to avoid importing CLI on every package import."""
    from synapse.cli import main as cli_main

    cli_main()


__all__ = ["__version__", "main"]
