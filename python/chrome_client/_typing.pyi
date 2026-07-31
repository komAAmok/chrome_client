import sys

if sys.version_info >= (3, 8):
    from typing import Literal
else:
    from typing_extensions import Literal

BrowserTypeLiteral = Literal[
    "chrome_99", "chrome_100", "chrome_101", "chrome_102", "chrome_103",
    "chrome_104", "chrome_105", "chrome_106", "chrome_107", "chrome_108",
    "chrome_109", "chrome_110", "chrome_111", "chrome_112", "chrome_113",
    "chrome_114", "chrome_115", "chrome_116", "chrome_117", "chrome_118",
    "chrome_119", "chrome_120", "chrome_121", "chrome_122", "chrome_123",
    "chrome_124", "chrome_125", "chrome_126", "chrome_127", "chrome_128",
    "chrome_129", "chrome_130", "chrome_131", "chrome_132", "chrome_133",
    "chrome_134", "chrome_135", "chrome_136", "chrome_137", "chrome_138",
    "chrome_139", "chrome_140", "chrome_141", "chrome_142", "chrome_143",
    "chrome_144", "chrome_145", "chrome_146", "chrome_147", "chrome_148",
    "chrome_149", "chrome_150", "chrome_151",
]
