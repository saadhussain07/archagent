import sys

from quiz.core import add_fractions

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        assert add_fractions("1/4", "1/4") == "1/2"
        print("SELFTEST OK")
