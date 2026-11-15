"""离线回放：开放规则或耗时未核实。不调用付费模型。"""

from evals.demo_replay import main

if __name__ == "__main__":
    import sys

    sys.argv = [sys.argv[0], "hours_unknown"]
    main()
