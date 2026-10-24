import argparse
from . import __version__


def main():
    parser = argparse.ArgumentParser(prog="agentbridge")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args()
