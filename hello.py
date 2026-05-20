#!/usr/bin/env python3
"""Test script: hello world with argparse."""
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--name", default="世界")
args = parser.parse_args()
print(f"你好, {args.name}!")
input("按 Enter 键退出...")
