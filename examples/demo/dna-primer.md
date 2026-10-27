---
title: DNA primer design — trajectory comparison
summary: Both agents chose the same primer pair; the harness program checks all 1.2M candidate pairs in 0.31 s.
about: Sub-report of the benchmark comparison
---

# DNA primer design — trajectory comparison

## Task {#task}

Design a primer pair for the given 2.4 kb template with Tm 58–62 °C, GC 40–60 %, and no 3' complementarity over 4 bases.

## Approaches {#approaches}

The recorded agent installed `primer3-py` and called it once. The harness agent wrote the nearest-neighbour Tm model in C and enumerated all candidate pairs.

## Outcome {#outcome}

Both selected `ATGCCGTTAGCGTACCTGA` / `TCAGGACTTGCAAGGCTA`. Tm values agree within 0.3 °C.
