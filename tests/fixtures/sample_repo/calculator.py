"""Sample calculator with a deliberate bug for agent demos."""


def add(a: int, b: int) -> int:
    return a + b


def sub(a: int, b: int) -> int:
    # BUG: should subtract; intentionally wrong for sample_repo demo.
    return a + b


def mul(a: int, b: int) -> int:
    return a * b
