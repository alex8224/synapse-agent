"""Fibonacci sequence — multiple implementations."""

from __future__ import annotations

import sys
from functools import lru_cache


# -------------------------------------------------------
# 1. Recursive (naive — exponential O(2^n))
# -------------------------------------------------------
def fib_recursive(n: int) -> int:
    if n <= 1:
        return n
    return fib_recursive(n - 1) + fib_recursive(n - 2)


# -------------------------------------------------------
# 2. Memoized recursive (top-down DP)
# -------------------------------------------------------
@lru_cache(maxsize=None)
def fib_memoized(n: int) -> int:
    if n <= 1:
        return n
    return fib_memoized(n - 1) + fib_memoized(n - 2)


# -------------------------------------------------------
# 3. Iterative (bottom-up DP, O(n) time, O(1) space)
# -------------------------------------------------------
def fib_iterative(n: int) -> int:
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b


# -------------------------------------------------------
# 4. Generator (lazy sequence)
# -------------------------------------------------------
def fib_generator(n: int):
    a, b = 0, 1
    for _ in range(n + 1):
        yield a
        a, b = b, a + b


# -------------------------------------------------------
# 5. Closed-form (Binet's formula)
#    F(n) = (phi^n - psi^n) / sqrt(5)
# -------------------------------------------------------
def fib_binet(n: int) -> int:
    phi = (1 + 5**0.5) / 2
    psi = (1 - 5**0.5) / 2
    return round((phi**n - psi**n) / 5**0.5)


# -------------------------------------------------------
# 6. Matrix exponentiation (O(log n))
# -------------------------------------------------------
def fib_matrix(n: int) -> int:
    if n <= 1:
        return n

    def mat_mul(a, b):
        return [
            a[0] * b[0] + a[1] * b[2],
            a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2],
            a[2] * b[1] + a[3] * b[3],
        ]

    def mat_pow(m, exp):
        result = [1, 0, 0, 1]  # identity
        base = list(m)
        while exp:
            if exp & 1:
                result = mat_mul(result, base)
            base = mat_mul(base, base)
            exp >>= 1
        return result

    m = [1, 1, 1, 0]
    powered = mat_pow(m, n - 1)
    return powered[0]


# -------------------------------------------------------
# Main
# -------------------------------------------------------
def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20

    implementations = [
        ("recursive     (O(2^n))", fib_recursive),
        ("memoized      (DP top-down)", fib_memoized),
        ("iterative     (DP bottom-up O(n))", fib_iterative),
        ("Binet         (closed-form)", fib_binet),
        ("matrix        (O(log n))", fib_matrix),
    ]

    print(f"Fibonacci sequence results for n=0..{n}:\n")

    # Print header
    header = f"{'n':>3}"
    col_labels = []
    for label, _ in implementations:
        short = label.split("(")[0].strip()
        col_labels.append(short)
        header += f"  {short:>8}"
    print(header)
    print("-" * len(header))

    # Generator version (lazy)
    gen_values = list(fib_generator(n))

    for i in range(n + 1):
        row = f"{i:>3}"
        row += f"  {fib_recursive(i):>8}"
        row += f"  {fib_memoized(i):>8}"
        row += f"  {fib_iterative(i):>8}"
        row += f"  {fib_binet(i):>8}"
        row += f"  {fib_matrix(i):>8}"
        print(row)

    # Verify all methods agree
    print(f"\nAll values from generator:", gen_values)
    print(f"All methods agree:", all(
        fib_recursive(i) == fib_memoized(i) == fib_iterative(i) == fib_binet(i) == fib_matrix(i) == gen_values[i]
        for i in range(n + 1)
    ))


if __name__ == "__main__":
    main()
