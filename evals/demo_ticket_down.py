"""离线回放：票务不可用。不调用付费模型。"""

from evals.demo_replay import main

if __name__ == "__main__":
    import sys

    sys.argv = [sys.argv[0], "ticket_down"]
    main()
