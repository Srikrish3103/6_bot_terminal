"""python -m pytest tests  (or: python tests/test_kst.py) — KST must match the hand-checked walkthrough."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.indicators import kst  # noqa: E402

PRICES = [996.84, 998.91, 1021.44, 1032.99, 1018.58, 1022.6, 1019.68, 1025.44, 1011.5, 1018.26, 1024.99, 1047.09,
          1054.95, 1065.15, 1052.0, 1082.71, 1064.39, 1081.68, 1082.08, 1075.95, 1072.49, 1068.86, 1077.64, 1080.65,
          1102.82, 1085.18, 1089.49, 1083.18, 1096.84, 1075.87, 1076.11, 1082.92, 1069.66, 1085.65, 1092.14, 1098.8,
          1104.91, 1087.62, 1072.74, 1065.0, 1068.44, 1073.11, 1059.46, 1047.16, 1032.83, 1021.1, 1013.35, 1006.83,
          1024.75, 1013.97, 1005.72, 1005.76, 990.31, 981.38, 976.63, 980.07, 961.17, 970.91, 962.33, 959.32, 974.13,
          987.14, 1004.72, 1012.69, 1018.56, 1028.27, 1023.19, 1043.19, 1040.55, 1032.4, 1062.38, 1067.17, 1059.75,
          1088.08, 1090.66, 1110.12, 1106.66, 1110.27, 1096.4, 1080.08, 1074.97, 1080.51, 1096.6, 1133.04, 1126.99,
          1146.43, 1145.68, 1150.65, 1138.86, 1127.23]


def test_kst_sma_matches_walkthrough():
    k, s = kst(pd.Series(PRICES))
    assert round(k.iloc[-1], 2) == 97.58 and round(s.iloc[-1], 2) == 87.74
    assert k.first_valid_index() + 1 == 45 and s.first_valid_index() + 1 == 53
    d = (k - s).dropna()
    crosses = [i + 1 for i in d.index[1:] if d[i - 1] < 0 < d[i]]
    assert crosses == [64]                       # bullish crossover on bar 64


def test_kst_ema_runs():
    k, s = kst(pd.Series(PRICES), method="ema")
    assert k.iloc[-1] > s.iloc[-1] > 0


if __name__ == "__main__":
    test_kst_sma_matches_walkthrough()
    test_kst_ema_runs()
    print("KST tests passed")
