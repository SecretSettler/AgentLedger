#!/usr/bin/env sh
# Build a throwaway store from the sample reports and serve it.
#   sh examples/build-demo.sh [target-dir]
# Sample numbers are illustrative only.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
demo="$here/demo"
target=${1:-"$here/../demo-store"}
export PYTHONPATH="$here/..${PYTHONPATH:+:$PYTHONPATH}"
al() { python3 -m agentledger --store "$target" "$@"; }

rm -rf "$target"
python3 -m agentledger init "$target" --name "Demo Ledger" >/dev/null
al project demo --title "Demo project" --description "Illustrative sample data for trying AgentLedger." >/dev/null
al publish demo/benchmark "$demo/benchmark-v1.md" --author claude-code
al comments import "$demo/comments-v1.csv"
al comments reply c_demo1 "Investigating: the harness re-emits the full C source on every call." --author claude-code
al publish demo/benchmark "$demo/benchmark-v2.md" --author claude-code \
  --changes "Explained the output-token gap (new subsection under Results) and re-tokenized the recorded logs." \
  --resolves c_demo1,c_demo2
al publish demo/benchmark/dna-primer "$demo/dna-primer.md" --author claude-code
al tree
echo
echo "open $target/index.html, or serve it with:"
echo "  PYTHONPATH=$here/.. python3 -m agentledger --store $target serve --open"
