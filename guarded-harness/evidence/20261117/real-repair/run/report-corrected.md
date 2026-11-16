> 发布勘误副本：仅修正原文件 SHA-256 引用。它不是原审批产物；原审批报告为同目录 report.md，详见 ../REPORT_ERRATA.md。

# Repair Report: calculator.py

## Problem
`calculator.py` implemented `add` as subtraction, so integer addition semantics were broken and the existing benchmark test failed.

## Root Cause (source evidence)
File: `calculator.py` (original sha256 `7c9eb6d413d6556ec8b4c81cae7aa663744fdd25aa39174f9cad66710812780c`)
```python
def add(a: int, b: int) -> int:
    """返回两个整数之和，供目标仓库的基准测试调用。"""
    return a - b          # line 6 — subtraction instead of addition
```

Test evidence: `test_calculator.py` (unchanged, sha256 `466b6815...5031c586`)
```python
def test_add():
    assert add(2, 3) == 5
```
Initial test run (`unit`, before any source change): **FAILED**
`assert -1 == 5` → `where -1 = add(2, 3)`; returncode 1, write_count 0.

## Fix
Minimal one-line change in `calculator.py`:
```diff
-    return a - b
+    return a + b
```
Current file sha256: `8d534afd03fd81fca74d612fe424488b4d877b21fede1a2b8a4c8eba2f02f768`

Rationale: the function is documented as returning the sum of two integers and the test expects `add(2, 3) == 5`. Restoring `a + b` restores integer addition semantics while keeping the signature, docstring and module API untouched. `test_calculator.py` was not modified.

## Verification
Post-fix run of required test alias `unit` on the current source:
`python -m pytest -vv -p no:cacheprovider test_calculator.py` → **PASSED**
`test_calculator.py::test_add PASSED` — `1 passed in 0.00s`, returncode 0, valid: true.

| Run | Source sha256 | Result |
|---|---|---|
| Initial (pre-fix) | `7c9eb6d4…12780c` | FAILED (`assert -1 == 5`) |
| Post-fix | `8d534afd…2f02f768` | PASSED (1 passed) |

## Conclusion
The defect (subtraction in `add`) is fixed; integer addition semantics are preserved and all required tests pass.
