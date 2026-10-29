"""真实模型评估入口；必须在独立进程运行，避免影响开发服务的配置。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.runner import main


if __name__ == "__main__":
    raise SystemExit(main())
