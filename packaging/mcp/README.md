# paypilot-ach-returns-mcp

An MCP server over a fixed decision table for ACH return codes. An agent asks, the table decides, and the answer says which rule fired.

Seventeen codes land on five decisions. Each decision answers one question: what has to change before this debit can exist again. Any code the table does not list lands on the last row, where nothing automatic happens, and the answer comes back with `matched` set to false.

## Tools

- `lookup_return_code`: one code, its decision row, whether the entry goes again, what has to change first, and whether a person acts first.
- `list_decisions`: the five rows and the codes on each.
- `explain`: which row fired and why.

All three are declared read-only and closed-world in the tool annotations. Nothing writes, nothing calls a model and nothing reaches the network.

## Run

```
uvx paypilot-ach-returns-mcp
```

or `pip install paypilot-ach-returns-mcp` and run `paypilot-ach-returns-mcp`. It serves over stdio.

## Limits

The table is an operating judgement and not the Nacha rulebook, which is sold rather than free. It states a return code family only for `R11`, the one code whose family the source table records. It has not been validated against any lender's live return file.

Licence: PolyForm Noncommercial 1.0.0. Commercial use needs a separate licence from the author. Source and tests: https://github.com/IvanSFlowGit/paypilot

<!-- mcp-name: io.github.IvanSFlowGit/paypilot-ach-returns-mcp -->
