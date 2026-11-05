#!/bin/zsh
cd /Users/vaibhav/Documents/Newproject/out/integration/b0
for c in a b c; do python3.12 -I run_b0_slices.py $c 2; done
date +%T
