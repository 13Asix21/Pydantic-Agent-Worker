"""Sandbox timeout fixture: sleeps long enough to exceed the test timeout."""

import time

if __name__ == "__main__":
    time.sleep(5)
    print("SLOW:DONE")
