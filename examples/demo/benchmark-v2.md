---
title: Harness agent vs. recorded agents on six benchmark tasks
summary: The harness agent passed all six hidden verifiers on its first finish, in 32 model calls, against the recorded agents' 98 environment calls. Its higher output-token count is mostly C source it must write out in full.
about: Benchmark comparison — harness vs. paper trajectories
tags: [benchmark, compile]
---

# Harness agent vs. recorded agents on six benchmark tasks

The outcome first: the harness agent passed all six tasks' hidden verifiers on its first `finish`, in 32 model calls, 210,172 output tokens and 41 minutes of wall time in all, against the recorded agents' 98 environment calls. Its final programs are 6 to 29 KB of C and run in 0.27 ms to 1.9 s of guest time.

## Setup {#setup}

Both agents start from the same instruction and the same input bytes. The first is the agent the paper ran in a CLI with a shell, Python, R and the network; its trajectory is replayed, not re-run. The second drives the harness: it may only submit a freestanding C program, which a fixed pipeline compiles and runs under KVM.

| Item | Value |
|---|---|
| Host | Xeon E5-2660 v3, 40 threads |
| Compiler | gcc 13.3, `-O2 -static -nostdlib` |
| Guest | KVM, 1 vCPU, 256 MB |

## Results {#results status=verified}

| Task | Harness out tok | Recorded out tok | Harness calls | Recorded env calls |
|:-----|---------------:|----------------:|-------------:|-------------------:|
| Scheduling | 61,204 | 9,876 | 7 | 4 |
| Log summary | 18,551 | 22,410 | 4 | 19 |
| DNA primer design | 33,902 | 30,118 | 6 | 23 |
| Bayesian network, Python | 41,377 | 28,004 | 6 | 21 |
| Bayesian network, R | 39,560 | 25,771 | 5 | 18 |
| Checkpoint diagnosis | 15,578 | 61,220 | 4 | 13 |

Where the two agents' computations are the same work, the programs run at the speed of the paper's artifacts. Where the recorded agent computed the answer in its head (the scheduling task) or reproduced a long diagnostic trajectory (the checkpoint task), the gap is explained rather than ratioed.

### Why harness output tokens run higher

The harness agent's output is dominated by program text: every call re-emits the full C file (6–29 KB, roughly 2,000–9,000 tokens), and the scheduling task needed seven iterations of a solver. The recorded agent's output is mostly short shell commands; its large inputs (logs, tool output) are *input* tokens and do not appear in this column. Excluding re-emitted source, harness output falls to 48,310 tokens, below the recorded 177,399.

### Per-task notes

- **Scheduling.** The recorded agent solved the constraint problem in prose and printed the answer; the harness had to write a solver.
- **Checkpoint diagnosis.** The recorded trajectory spends most of its tokens reading logs that the harness program parses directly.

## Token accounting {#token-accounting}

Output tokens are counted with the same tokenizer for both agents; the recorded agent's counts were re-tokenized from the paper's logs.

## Compile vs. patch {#compile-vs-patch}

Every harness turn recompiles the whole program. Incremental patching would cut compile time, but compile time is already under 2% of wall time.

## Limits {#limits}

1. One run per task; no variance estimate yet.
2. Scheduling remains the outlier: a solver is more text than an answer.
