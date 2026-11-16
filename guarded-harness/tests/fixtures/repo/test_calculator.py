"""目标仓库的整数加法基准测试。"""

from calculator import add


def test_add():
    """验证基准输入的加法结果，作为目标仓库的通过条件。"""
    assert add(2, 3) == 5
