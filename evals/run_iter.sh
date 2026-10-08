#!/bin/bash
# usage: evals/run_iter.sh N   -> runs the standard four simulations for iteration N
cd "$(dirname "$0")/.." || exit 1
SIM_TURNS=${SIM_TURNS:-18} venv/bin/python evals/sim.py "i$1" aws:2:passive lsat:2:struggling quant:2:curious aws:3:overconfident 2>&1 | grep -v Warn
