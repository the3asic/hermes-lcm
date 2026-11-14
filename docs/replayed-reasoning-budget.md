# Retained reasoning and the assembly budget

LCM prices retained canonical messages with the compatible Hermes Core's replay rules.
The required host APIs are recorded in `dependency-contract.json`; the source version
range alone does not establish that a host exposes them. Older hosts can count
ordinary text. Attempting to budget retained reasoning without those real APIs
raises an explicit compatibility error. Standalone development helpers never
supply replacement replay-pricing APIs.

The visible content and tool calls retain LCM's token counting. A nonempty `api_content`
sidecar on a user or assistant message supplies the text the provider receives.
Codex and xAI encrypted reasoning use the host's per-message provider-reported token
stamp, with the host's documented approximation for older messages. Compaction
checkpoints retain the host's separate accounting rule. Ciphertext length is not
passed through the text tokenizer.

Native Anthropic thinking is projected across the complete retained message list.
The host decides which signed carriers survive their surrounding message boundaries;
their reasoning cost is added once, separately from visible content. Counting one
message uses a single-message view; final assembly counts the complete list.
Tail selection and final assembly evaluate each candidate list, including complete
tool exchanges and consecutive assistant rows. The active Anthropic or Responses
API mode selects its own carrier. An unspecified route uses the maximum of
alternative carrier costs on a row, never their sum. A selection-local payload
cache avoids re-tokenizing unchanged long content; the host re-evaluates carrier
validity for each candidate, and the cache never survives message mutation.

Overflow recovery keeps its existing independent assembly cap. Its stripped replay
carriers and reasoning stamps are removed together and have no reasoning surcharge.
Display metadata and persistent row identities do not become provider text.

Release validation must pair the final Core and LCM commits. Check ordinary tails,
complete tool exchanges, cold replay and both recovery paths using real recorded
messages. Preserve the recorded reasoning counts through gateway replay, and compare
LCM's selected context with the final provider projection. Source checks and successful
startup are separate from a real threshold-triggered compression and continuation.

The offline operator entrypoint is `scripts/validate_recorded_host.py --core CORE
--records RECORDINGS.json --output OUTPUT`. Supply real persisted records in the
export's `core_rows`, `signed_rows`, `store_rows` and `summary_nodes` collections.
These private inputs stay in ignored artifacts. The receipt identifies exact
source hashes and explicitly records when provider-reported stamps or live L1/L2
model output were unavailable. Historical estimates never certify exact stamped
cold-replay pricing.
