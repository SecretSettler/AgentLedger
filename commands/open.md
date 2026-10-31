---
description: Open the AgentLedger review page in the browser (comments and Ask)
argument-hint: "[report id]"
---

Run `agentledger open $ARGUMENTS` in the background (it serves the store on http://localhost:8765 and opens the page).
If `agentledger` is not installed, follow the setup step in the agentledger skill first. Tell the user the URL, and that
on a remote machine they can forward it with `ssh -L 8765:localhost:8765 <host>`.
