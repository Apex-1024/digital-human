"""避开 PowerShell ^ 转义问题，直接用文件方式测 latex_to_chinese"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
from core import latex_to_chinese

cases = [
    r"\int_a^b f(x) dx",
    r"\frac{a}{b}",
    r"\sqrt{x+1}",
    r"x^2 + y^2 = r^2",
    r"\sum_{i=1}^{n} a_i",
    r"\lim_{x \to 0} \frac{\sin x}{x}",
    r"\Phi(x) = \int_a^x f(t) dt",
    r"\alpha + \beta = \gamma",
]
for c in cases:
    print(repr(c))
    print("  ->", repr(latex_to_chinese(c)))
