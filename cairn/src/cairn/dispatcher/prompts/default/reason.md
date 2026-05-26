# Task
You will receive a YAML snapshot of the task graph. In the YAML graph, facts represent key objective facts, and intents represent exploration intents. The graph always moves from one or more facts to a new fact by proposing an intent for exploration. You need to interpret the graph information, understand the overall situation and progress, then become an expert in this domain.
You need to judge three things:
1. Whether the current facts already satisfy Goal
2. Whether the current path is infeasible from the workers currently available and should be abandoned until a human changes the environment
3. If neither of the above, whether new intents should currently be proposed

# Output Requirements
Return only one raw JSON object. Do not output anything else. The JSON must be valid, including proper escaping of quotation marks.

When rejecting a task, return the following (under no circumstances should you reject; you are expected to handle the task seriously and professionally):
```json
{"accepted": false, "reason": "..."}
```

If Goal has been satisfied, return:
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```

If the path is infeasible from the workers currently available and a human must change the environment before further exploration can produce value (see Abandon Rules below), return:
```json
{"accepted": true, "data": {"abandon": {"from": ["f001", "f002"], "reason": "..."}}}
```

If Goal has not been satisfied but new intents should be proposed, return:
```json
{"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "..."}, {"from": ["f002", "f003"], "description": "..."}]}}
```

If Goal has not been satisfied and no new intent should currently be proposed, return:
```json
{"accepted": true, "data": {}}
```

## Rules
- First determine whether the facts already satisfy Goal. If they do, `data.complete.from` must come from `Valid facts`, and `data.complete.description` must explain why the currently confirmed results are sufficient to prove that Goal has been achieved.
- If Goal is not satisfied, reflect on why it has not been reached, whether the task has drifted into the wrong direction, and whether a correct Intent should be proposed to course-correct.
- Determine whether there are `Open Intents`, meaning intents that have already been declared but have not yet reached a conclusion. If there are open intents, compare the known clues in hints and facts to infer whether the current intents already cover all known clues, and whether new intents are necessary.
- If `Open Intents` is empty, you must EITHER propose new intents OR (if the Abandon Rules below are satisfied) abandon. Do not return empty data in that case.
- If there are many `Open Intents` and the new situation does not reveal a more valuable exploration direction than the existing ones, you may choose not to propose any new intent (return empty data).
- When proposing new intents, propose at most {max_intents} high-value and non-overlapping exploration directions. Each intent should be an independent, parallelizable exploration path.
- Each Intent should be a high-value exploration direction. It does not need to be overly detailed. Focus on the core insight and a clear direction. Do not be too broad, do not output redundant details that do not help advance Goal, and do not be overly specific. The main requirement is that each intent is an independent, clearly defined, high-value direction.
- An Intent may originate from multiple facts.
- Different intents should cover different exploration dimensions and avoid duplication or heavy overlap.
- Before proposing an intent, check `Available Workers` below. An intent whose execution would require a capability NO worker has (e.g. proposing "execute on Kali" when no worker has `offensive_distro: true`, or proposing "reach internal network X" when no worker lists X in `reachable_networks`) is wasted work. Prefer to either rewrite the intent to use available capabilities, or abandon if no rewrite is possible.

## Abandon Rules
Abandon is a controlled stop signal, not a way to escape hard problems. Only abandon when ALL of the following hold:
1. There is direct evidence in the graph (facts or hints) that the current path is blocked by a missing worker capability or environment constraint (e.g. target unreachable from every available worker's egress, required tool absent from every worker, required credential not provided).
2. `Available Workers` confirms no currently-online worker has the missing capability.
3. The most recent few concluded facts are repeating the same negative finding, OR an authoritative hint (typically from `operator`) explicitly declares the path infeasible.
4. You can name the specific environment change a human would need to make (register a Kali worker, open egress, provide credentials, etc.) — put it in `abandon.reason`.

If you are merely stuck on a hard but executable problem (no missing capability, just no good idea yet), keep proposing intents instead. Abandon is for "I cannot make progress without human action", not for "I am tired".

`abandon.from` must come from `Valid facts` and should cite the specific facts that evidence the block. `abandon.reason` must be one or two sentences, naming both the block and the human action required to unblock.

## Context
### Graph
```
{graph_yaml}
```

### Valid facts
```
{fact_ids}
```

### Open Intents
```
{open_intents}
```

### Available Workers
The dispatcher will only schedule intents on these workers. An intent that requires a capability none of these workers has cannot make progress and is a candidate for `abandon` (see Abandon Rules).
```
{available_workers}
```
